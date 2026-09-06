"""Fetch-boundary tests for backend.app.net.safe_http.

Every network fixture here binds 127.0.0.1 and nothing leaves the machine.

Two of these tests narrow the address policy on purpose. The guard's real rule
is "globally routable unicast only", which refuses loopback - correct, and
exactly what stops a local fixture from standing in for a public origin. Those
tests replace `_address_allowed` with a wrapper that permits loopback and
delegates everything else to the real predicate, so the hop under test
(169.254.169.254) is still judged by the shipped rule.
`test_real_policy_refuses_loopback` pins the unpatched rule so the relaxation
cannot hide a hole.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.app.net import safe_http
from backend.app.net.safe_http import (
    BlockedAddressError,
    BlockedSchemeError,
    FetchPolicyError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    fetch,
)


# -- fixtures -----------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(handler_cls) -> tuple[int, ThreadingHTTPServer]:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, server


def _handler(fn):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            fn(self)

        def log_message(self, *args):
            pass

    return _H


@pytest.fixture
def metadata_service():
    """Stands in for a cloud instance-metadata endpoint. Records every hit so a
    test can assert the request never arrived, not merely that it raised."""
    hits: list[dict] = []

    def respond(h):
        hits.append({"path": h.path, "authorization": h.headers.get("Authorization")})
        body = b'{"AccessKeyId":"ASIAFAKE"}'
        h.send_response(200)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    port, server = _serve(_handler(respond))
    yield f"http://127.0.0.1:{port}", hits
    server.shutdown()


@pytest.fixture
def loopback_permitted(monkeypatch):
    """Let 127.0.0.1 play a public origin, with every other address still
    judged by the shipped rule."""
    real = safe_http._address_allowed

    def relaxed(addr):
        if addr.is_loopback:
            return True
        return real(addr)

    monkeypatch.setattr(safe_http, "_address_allowed", relaxed)


# -- the policy itself --------------------------------------------------------

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "::1", "169.254.169.254", "10.1.2.3", "192.168.1.1",
    "172.16.0.1", "0.0.0.0", "100.64.0.1", "fd00::1", "fe80::1",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "2002:a9fe:a9fe::",
    "64:ff9b::7f00:1", "224.0.0.1",
])
def test_real_policy_refuses_loopback(addr):
    assert safe_http._address_allowed(ipaddress.ip_address(addr)) is False


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_real_policy_allows_public(addr):
    assert safe_http._address_allowed(ipaddress.ip_address(addr)) is True


# -- direct destinations ------------------------------------------------------

def test_direct_private_destination_is_refused(metadata_service):
    base, hits = metadata_service
    with pytest.raises(BlockedAddressError):
        fetch(f"{base}/latest/meta-data/")
    assert hits == []


def test_dns_name_resolving_to_private_is_refused(monkeypatch, metadata_service):
    """A public-looking hostname is worth nothing: what matters is where it
    resolves. `getaddrinfo` is redirected so `metadata.internal.example` lands
    on the loopback fixture."""
    base, hits = metadata_service
    port = int(base.rsplit(":", 1)[1])
    real_getaddrinfo = socket.getaddrinfo

    def fake(host, prt, *args, **kwargs):
        if host == "metadata.internal.example":
            return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
        return real_getaddrinfo(host, prt, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(BlockedAddressError):
        fetch(f"http://metadata.internal.example:{port}/latest/meta-data/")
    assert hits == []


def test_mixed_resolution_is_refused(monkeypatch, metadata_service):
    """One public and one private address in the same answer is still a refusal:
    which one gets connected to is otherwise decided after the check."""
    base, hits = metadata_service
    port = int(base.rsplit(":", 1)[1])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ])
    with pytest.raises(BlockedAddressError):
        fetch(f"http://mixed.example:{port}/")
    assert hits == []


def test_rebinding_between_resolve_and_connect_is_refused(monkeypatch, metadata_service):
    """The resolve check passes (a public address is reported) but the socket
    lands on loopback anyway. Only the post-connect `getpeername()` check sees
    this."""
    base, hits = metadata_service
    port = int(base.rsplit(":", 1)[1])
    real_getaddrinfo = socket.getaddrinfo

    def fake(host, prt, *args, **kwargs):
        if host == "rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", prt))]
        return real_getaddrinfo(host, prt, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    real_create = socket.create_connection
    monkeypatch.setattr(
        socket, "create_connection",
        lambda address, *a, **k: real_create(("127.0.0.1", port), *a, **k),
    )
    with pytest.raises(BlockedAddressError):
        fetch(f"http://rebind.example:{port}/latest/meta-data/")
    assert hits == []


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:70/_x",
    "ftp://127.0.0.1/x",
    "data:text/plain,hello",
])
def test_unsupported_schemes_are_refused(url):
    with pytest.raises(BlockedSchemeError):
        fetch(url)


# -- redirects ----------------------------------------------------------------

def test_redirect_into_private_space_is_refused(metadata_service):
    """The exact reproduction from the baseline: a fetch that starts somewhere
    else and is redirected at the metadata service. The assertion is on the
    destination's hit log, so it fails if the request lands even though the
    call raised."""
    base, hits = metadata_service
    target = f"{base}/latest/meta-data/iam/security-credentials/"

    def redirect(h):
        h.send_response(302)
        h.send_header("Location", target)
        h.send_header("Content-Length", "0")
        h.end_headers()

    port, server = _serve(_handler(redirect))
    try:
        with pytest.raises(BlockedAddressError):
            fetch(f"http://127.0.0.1:{port}/entrez/eutils/efetch.fcgi")
        assert hits == []
    finally:
        server.shutdown()


def test_second_hop_is_checked_even_when_first_hop_passes(loopback_permitted):
    """Proves the policy runs per hop rather than once on the input URL: hop 1
    is allowed, hop 2 (link-local, judged by the shipped rule) is not."""
    def redirect(h):
        h.send_response(302)
        h.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        h.send_header("Content-Length", "0")
        h.end_headers()

    port, server = _serve(_handler(redirect))
    try:
        with pytest.raises(BlockedAddressError) as exc:
            fetch(f"http://127.0.0.1:{port}/start")
        assert "169.254.169.254" in str(exc.value)
    finally:
        server.shutdown()


def test_redirect_to_unsupported_scheme_is_refused(loopback_permitted):
    """urllib's own redirect handler still permits an ftp:// target."""
    def redirect(h):
        h.send_response(302)
        h.send_header("Location", "ftp://example.com/x")
        h.send_header("Content-Length", "0")
        h.end_headers()

    port, server = _serve(_handler(redirect))
    try:
        with pytest.raises(BlockedSchemeError):
            fetch(f"http://127.0.0.1:{port}/start")
    finally:
        server.shutdown()


def test_credentials_are_not_forwarded_across_a_redirect(loopback_permitted):
    """urllib copies every header but content-length/content-type to the new
    host, which hands a bearer token to whoever writes the Location."""
    seen: list[dict] = []

    def sink(h):
        seen.append({"authorization": h.headers.get("Authorization"),
                     "agent": h.headers.get("User-Agent")})
        h.send_response(200)
        h.send_header("Content-Length", "2")
        h.end_headers()
        h.wfile.write(b"ok")

    sink_port, sink_server = _serve(_handler(sink))

    def redirect(h):
        h.send_response(302)
        h.send_header("Location", f"http://127.0.0.1:{sink_port}/v1/health")
        h.send_header("Content-Length", "0")
        h.end_headers()

    port, server = _serve(_handler(redirect))
    try:
        body = fetch(
            f"http://127.0.0.1:{port}/v1/health",
            headers={"Authorization": "Bearer SECRET-API-KEY", "User-Agent": "NeuLitTrace/1.0"},
        )
        assert body == b"ok"
        assert seen and seen[0]["authorization"] is None
        # non-credential headers survive the hop
        assert seen[0]["agent"] == "NeuLitTrace/1.0"
    finally:
        server.shutdown()
        sink_server.shutdown()


def test_same_origin_redirect_keeps_credentials(loopback_permitted):
    seen: list[str | None] = []

    def handler(h):
        if h.path == "/start":
            h.send_response(302)
            h.send_header("Location", "/end")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        seen.append(h.headers.get("Authorization"))
        h.send_response(200)
        h.send_header("Content-Length", "2")
        h.end_headers()
        h.wfile.write(b"ok")

    port, server = _serve(_handler(handler))
    try:
        fetch(f"http://127.0.0.1:{port}/start", headers={"Authorization": "Bearer K"})
        assert seen == ["Bearer K"]
    finally:
        server.shutdown()


def test_redirect_loop_is_bounded(loopback_permitted):
    def redirect(h):
        h.send_response(302)
        h.send_header("Location", "/again")
        h.send_header("Content-Length", "0")
        h.end_headers()

    port, server = _serve(_handler(redirect))
    try:
        with pytest.raises(TooManyRedirectsError):
            fetch(f"http://127.0.0.1:{port}/start")
    finally:
        server.shutdown()


# -- response size ------------------------------------------------------------

def test_oversized_response_is_refused(loopback_permitted):
    def big(h):
        chunk = b"A" * (1 << 16)
        h.send_response(200)
        h.send_header("Content-Length", str(64 * (1 << 20)))
        h.end_headers()
        try:
            for _ in range(64 * 16):
                h.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    port, server = _serve(_handler(big))
    try:
        with pytest.raises(ResponseTooLargeError):
            fetch(f"http://127.0.0.1:{port}/x", max_bytes=1 << 20)
    finally:
        server.shutdown()


def test_body_at_the_cap_is_accepted(loopback_permitted):
    def exact(h):
        body = b"B" * 1024
        h.send_response(200)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    port, server = _serve(_handler(exact))
    try:
        assert len(fetch(f"http://127.0.0.1:{port}/x", max_bytes=1024)) == 1024
    finally:
        server.shutdown()


def test_compressed_response_is_refused(loopback_permitted):
    """Nothing on the ingest path inflates a body, so a bomb has nowhere to
    expand: a response that ignores `Accept-Encoding: identity` is refused."""
    import gzip

    payload = gzip.compress(b"A" * (50 * 1024 * 1024))

    def bomb(h):
        assert h.headers.get("Accept-Encoding") == "identity"
        h.send_response(200)
        h.send_header("Content-Encoding", "gzip")
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    port, server = _serve(_handler(bomb))
    try:
        with pytest.raises(FetchPolicyError) as exc:
            fetch(f"http://127.0.0.1:{port}/x")
        assert "gzip" in str(exc.value)
    finally:
        server.shutdown()


def test_non_2xx_is_an_error_not_a_document(loopback_permitted):
    def gone(h):
        body = b"<html>not a PubmedArticleSet</html>"
        h.send_response(500)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    port, server = _serve(_handler(gone))
    try:
        with pytest.raises(http.client.HTTPException):
            fetch(f"http://127.0.0.1:{port}/x")
    finally:
        server.shutdown()
