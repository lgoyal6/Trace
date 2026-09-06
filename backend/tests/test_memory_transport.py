"""The two EverOS clients that were bypassing the safe_http boundary.

`backend/memory/evermind.py` (httpx) and `backend/memex/everos_client.py`
(stdlib urllib) both send `Authorization: Bearer <key>` on every call, and
`EVEROS_BASE_URL` is an unvalidated environment variable. `everos_client.py`
used bare `urllib.request.urlopen`, which follows up to 10 redirects and whose
`HTTPRedirectHandler.redirect_request` copies request headers onto the target,
so a 302 handed the API key to whoever wrote the Location header.

`safe_http.fetch` cannot be used by either: it is GET-only and both clients
POST/PUT/DELETE. What is shared instead is the policy, via
`safe_http.assert_destination_allowed`, plus a hard no-redirect rule.

TEST SEAM, stated so nobody has to rediscover it: the redirect test below runs
against two loopback servers, which the shipped address policy correctly
refuses. That one test patches `_destination_allowed` to True so the redirect
behaviour can be observed in isolation. `test_the_real_policy_refuses_loopback`
pins the unpatched predicate so the relaxation cannot hide a hole.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.app.net.safe_http import (
    BlockedAddressError,
    BlockedSchemeError,
    assert_destination_allowed,
)


# ---------------------------------------------------------------------------
# The shared policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/v1",
        "http://localhost:9999/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8080/v1",
    ],
)
def test_the_real_policy_refuses_a_private_destination(url):
    with pytest.raises(BlockedAddressError):
        assert_destination_allowed(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/1"])
def test_the_real_policy_refuses_a_non_http_scheme(url):
    with pytest.raises(BlockedSchemeError):
        assert_destination_allowed(url)


def test_the_real_policy_refuses_loopback():
    """Pinned separately, because one test below relaxes exactly this."""
    from backend.memex.everos_client import _destination_allowed as memex_check
    from backend.memory.evermind import _destination_allowed as evermind_check

    assert memex_check("http://127.0.0.1:1/v1") is False
    assert evermind_check("http://127.0.0.1:1/v1") is False


# ---------------------------------------------------------------------------
# evermind.py (httpx)
# ---------------------------------------------------------------------------


def test_evermind_disables_itself_when_the_base_url_is_a_private_address(monkeypatch):
    """A base URL in private space plus a bearer token is a credential handed
    to whatever is listening there. The class already promises to degrade, so
    it degrades rather than raising."""
    from backend.memory.evermind import EverOSMemory

    monkeypatch.setenv("EVEROS_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("EVEROS_API_KEY", "SECRET-API-KEY")

    memory = EverOSMemory()
    assert memory._configured is False
    assert memory._client is None
    assert memory.health()["ok"] is False
    # And it still satisfies MemoryPort: reads return empty defaults.
    assert memory.get_profile("u").specialty is None
    assert memory.seen_pmids("u") == set()
    memory.forget("u")  # must not raise


def test_evermind_never_follows_a_redirect(monkeypatch):
    """httpx defaults to follow_redirects=False, but it is asserted rather
    than assumed: httpx copies request headers onto a redirect target, so a
    flipped default would leak the bearer token."""
    from backend.memory.evermind import EverOSMemory

    monkeypatch.setenv("EVEROS_BASE_URL", "https://api.evermind.ai")
    monkeypatch.setenv("EVEROS_API_KEY", "SECRET-API-KEY")
    monkeypatch.setattr("backend.memory.evermind._destination_allowed", lambda url: True)

    memory = EverOSMemory()
    assert memory._client is not None
    assert memory._client.follow_redirects is False


# ---------------------------------------------------------------------------
# everos_client.py (urllib) - the one that really did leak
# ---------------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    """Records what arrived, so the assertion is on the destination's own log
    rather than on an inferred code path."""

    seen: list[dict] = []
    redirect_to: str | None = None

    def _record(self, method: str):
        type(self).seen.append({
            "method": method,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
        })

    def do_GET(self):  # noqa: N802
        # urllib turns a 302 on a POST into a GET on the target, so a server
        # that only implements do_POST answers 501 and records nothing - the
        # leaked credential would arrive and never appear in the log. This
        # handler exists because a negative control caught exactly that.
        self._record("GET")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._record("POST")
        if type(self).redirect_to:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # silence the test log
        pass


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_everos_client_refuses_a_redirect_instead_of_resending_the_bearer_token(monkeypatch):
    """The measured leak, closed. Two loopback servers: the first 302s to the
    second. The bearer token must never appear in the second server's log."""

    class First(_Recorder):
        seen: list[dict] = []

    class Second(_Recorder):
        seen: list[dict] = []

    first, second = _serve(First), _serve(Second)
    try:
        First.redirect_to = f"http://127.0.0.1:{second.server_port}/v2/stolen"
        Second.redirect_to = None

        monkeypatch.setenv("EVEROS_API_KEY", "SECRET-API-KEY")
        monkeypatch.setenv("EVEROS_BASE_URL", f"http://127.0.0.1:{first.server_port}")
        monkeypatch.delenv("MEMEX_MOCK", raising=False)
        # See TEST SEAM in the module docstring: loopback is correctly refused
        # by the shipped policy, so this test relaxes that one check in order
        # to observe the redirect behaviour on its own.
        monkeypatch.setattr("backend.memex.everos_client._destination_allowed", lambda url: True)

        from backend.memex.everos_client import EverOSMemory as MemexEverOS

        client = MemexEverOS()
        client._mock = False
        result = client._post("/api/v2/memory/search", {"query": "x"})

        assert First.seen, "the first hop should have been made"
        assert First.seen[0]["authorization"] == "Bearer SECRET-API-KEY"
        assert Second.seen == [], f"bearer token followed the redirect: {Second.seen}"
        assert result is None, "a refused redirect must degrade, not raise"
    finally:
        First.redirect_to = None
        first.shutdown()
        second.shutdown()


def test_everos_client_does_not_send_the_key_to_a_private_destination(monkeypatch):
    """With the policy check in place (not relaxed), the request is never
    written at all."""

    class Only(_Recorder):
        seen: list[dict] = []

    server = _serve(Only)
    try:
        Only.redirect_to = None
        monkeypatch.setenv("EVEROS_API_KEY", "SECRET-API-KEY")
        monkeypatch.setenv("EVEROS_BASE_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.delenv("MEMEX_MOCK", raising=False)

        from backend.memex.everos_client import EverOSMemory as MemexEverOS

        client = MemexEverOS()
        client._mock = False
        assert client._post("/api/v2/memory/search", {"query": "x"}) is None
        assert Only.seen == [], f"request reached a private destination: {Only.seen}"
    finally:
        server.shutdown()
