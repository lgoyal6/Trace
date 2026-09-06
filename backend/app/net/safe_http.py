"""Outbound HTTP for the ingest path, policed at the socket rather than at
the URL string.

Trace pulls documents it does not control (PubMed efetch/esearch) and talks to
memory services over URLs that come from environment configuration. Both are
places where "just fetch this URL" turns into a request the deployment never
intended: a redirect that starts on a public host and lands on
169.254.169.254, a DNS name that resolves into 10.0.0.0/8, an `Authorization`
header carried along to whoever the redirect names.

Checking the *input* URL cannot catch any of that. A hostname says nothing
about where it resolves, and the string the caller passed is not the string
the last hop used. So the checks here happen in two places that a redirect or
a DNS answer cannot route around:

  1. before each hop, on every address `getaddrinfo` returns for that hop's
     host - all of them, so an answer mixing one public and one private
     address is refused rather than raced; and
  2. after `connect()`, on `getpeername()` - the address the kernel actually
     opened, which is what closes the gap between resolving a name and
     connecting to it (DNS rebinding).

Redirects are followed by hand, one hop at a time, so rule 1 and rule 2 apply
to every hop and not just the first. `Authorization`, `Cookie` and
`Proxy-Authorization` are dropped the moment the origin changes: urllib's own
redirect handler copies every header except content-length/content-type
straight to the new host, which hands a bearer token to whoever controls the
`Location`.

Responses are capped (`max_bytes`) and requested as `Accept-Encoding:
identity`; a body that arrives compressed anyway is refused rather than
inflated, so there is nothing here for a decompression bomb to expand into.
"""
from __future__ import annotations

import http.client
import ipaddress
import logging
import socket
import ssl
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger("neulit.net.safe_http")

#: 8 MiB. PubMed efetch for a whole condition is a few hundred KB; this is
#: headroom, not a target.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 30.0

_ALLOWED_SCHEMES = ("http", "https")
_CREDENTIAL_HEADERS = ("authorization", "cookie", "proxy-authorization")


class FetchPolicyError(RuntimeError):
    """Base for every refusal made by this module rather than by the network."""


class BlockedSchemeError(FetchPolicyError):
    pass


class BlockedAddressError(FetchPolicyError):
    pass


class ResponseTooLargeError(FetchPolicyError):
    pass


class TooManyRedirectsError(FetchPolicyError):
    pass


def _address_allowed(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally routable unicast address.

    Allowlisting `is_global` rather than denylisting known-bad ranges is what
    makes this hold for the cases a hand-written denylist forgets: CGNAT
    (100.64.0.0/10), 6to4 and Teredo addresses that wrap a private v4 address,
    0.0.0.0, and the reserved space that NAT64 (64:ff9b::/96) lives in.
    Multicast and reserved are excluded explicitly because a few of those
    ranges still report `is_global`.
    """
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_global and not addr.is_multicast and not addr.is_reserved)


def _assert_address_allowed(raw: str, *, host: str) -> None:
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError as exc:  # not an address we can classify: refuse it
        raise BlockedAddressError(f"unclassifiable address {raw!r} for host {host!r}") from exc
    if not _address_allowed(addr):
        raise BlockedAddressError(
            f"host {host!r} resolves to non-public address {raw}; refusing to connect"
        )


def _resolve_and_check(host: str, port: int) -> None:
    """Refuse the hop unless *every* address `host` resolves to is public.

    All of them, not the first: a resolver that returns one public and one
    private address would otherwise be a coin flip decided after the check.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedAddressError(f"cannot resolve host {host!r}: {exc}") from exc
    if not infos:
        raise BlockedAddressError(f"host {host!r} resolved to no addresses")
    for info in infos:
        _assert_address_allowed(info[4][0], host=host)


def _peer_checking(connection_cls):
    """Wrap an http.client connection class so `connect()` re-checks the peer.

    This is the check that survives DNS rebinding: `_resolve_and_check` looked
    at an answer, this looks at the socket. The request is not written until
    it passes, so a rebound name costs one TCP handshake and nothing else.
    """

    class _Checked(connection_cls):  # type: ignore[valid-type, misc]
        def connect(self) -> None:
            super().connect()
            try:
                peer = self.sock.getpeername()[0]
                _assert_address_allowed(peer, host=self.host)
            except Exception:
                self.close()
                raise

    return _Checked


def _split_scheme(url: str) -> tuple[str, str, int, str]:
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise BlockedSchemeError(
            f"scheme {parts.scheme!r} is not fetchable; only {'/'.join(_ALLOWED_SCHEMES)} are"
        )
    if not parts.hostname:
        raise BlockedSchemeError(f"url {url!r} has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return parts.scheme, parts.hostname, port, path


def _origin(url: str) -> tuple[str, str, int]:
    scheme, host, port, _ = _split_scheme(url)
    return scheme, host.lower(), port


def _strip_credentials(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS}


def assert_destination_allowed(url: str) -> None:
    """Apply this module's scheme and address policy to a URL that some OTHER
    client will fetch.

    `fetch` is GET-only, so the two EverOS clients (which POST, PUT and
    DELETE) cannot use it. They can still use its policy: this runs the same
    scheme allowlist and the same "every resolved address must be globally
    routable" check that `fetch` runs on each hop. What it does NOT provide is
    the post-connect `getpeername` check, because the socket belongs to the
    other client - so this is a pre-flight check, weaker than `fetch`, and the
    docstring says so rather than letting a caller assume parity.

    Raises `BlockedSchemeError` / `BlockedAddressError`. Callers that must
    degrade rather than raise should catch `FetchPolicyError`.
    """
    scheme, host, port, _ = _split_scheme(url)
    _resolve_and_check(host, port)


def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> bytes:
    """GET `url` and return at most `max_bytes` of body.

    Raises a `FetchPolicyError` subclass when this module refuses the request,
    and the usual `OSError`/`http.client` errors when the network does. A
    non-2xx status raises `http.client.HTTPException`, so callers see a failed
    fetch rather than an error page parsed as a document.
    """
    current = url
    request_headers = dict(headers or {})
    for _ in range(max_redirects + 1):
        scheme, host, port, path = _split_scheme(current)
        _resolve_and_check(host, port)

        if scheme == "https":
            conn_cls = _peer_checking(http.client.HTTPSConnection)
            conn = conn_cls(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            conn_cls = _peer_checking(http.client.HTTPConnection)
            conn = conn_cls(host, port, timeout=timeout)

        try:
            # identity, so nothing arrives that we would have to inflate to
            # read. A body that ignores this is refused below.
            conn.request("GET", path, headers={**request_headers, "Accept-Encoding": "identity"})
            response = conn.getresponse()
            status = response.status
            location = response.getheader("Location")
            encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()

            if status in (301, 302, 303, 307, 308) and location:
                response.read(1)  # drain enough to release the connection
                target = urljoin(current, location)
                # _split_scheme rejects file://, gopher:// and friends here as
                # well as on the first hop, which urllib does not: its own
                # handler still permits an ftp:// redirect target.
                if _origin(target) != (scheme, host.lower(), port):
                    request_headers = _strip_credentials(request_headers)
                current = target
                continue

            if status < 200 or status >= 300:
                raise http.client.HTTPException(f"GET {current} returned HTTP {status}")
            if encoding != "identity":
                raise FetchPolicyError(
                    f"GET {current} returned Content-Encoding {encoding!r}; "
                    "only identity is accepted on the ingest path"
                )

            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ResponseTooLargeError(
                    f"GET {current} returned more than {max_bytes} bytes"
                )
            return body
        finally:
            conn.close()

    raise TooManyRedirectsError(f"more than {max_redirects} redirects starting at {url}")
