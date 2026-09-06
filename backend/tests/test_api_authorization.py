"""Authorization, tested independently of the code path that grants access.

**The finding this file exists to record: Trace authenticates nothing.**

Unlike a service with no principal at all, Trace has a principal *identifier*:
`user_id` names whose memory a request reads, writes or destroys. It is supplied by
the caller, in a query string or a JSON body, and nothing checks it. There is no
`securitySchemes` in the committed contract, no operation carries a `security`
requirement, and no route takes a credential. Presenting another user's identifier is
therefore not refused - it is granted, in full, including `POST /memory/forget`, which
destroys that user's stored memory and answers 204.

The tests below say that in assertions rather than in prose, because a reader who wants
to know whether Trace authorizes anything should get the answer from a test result.
Nothing here should be read as evidence that access control works; there is none.

What Trace does implement, and what is worth holding still, is **scoping**: a request
naming user B must not read or write user A's rows. It is a correctness property, not a
security one - with no authenticated principal there is nothing to be authorized - but
it is the boundary that exists, and it is the boundary an identity model would have to
be enforced on top of.

**Independence.** No assertion here reads the answer back through the route that served
the request. A scoping bug in the route layer checked by the same layer agrees with
itself. Every check is made against `services.memory`'s own storage, which the request
never passes through. `backend/tests/isolated_server.py` exposes the same rows over
`GET /__memory_store` for the HTTP harness, and for the same reason.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.limiter import limiter
from backend.api.main import app
from backend.contracts.registry import get_services

SPEC = json.loads(
    (Path(__file__).resolve().parents[2] / "frontend" / ".openapi.json").read_text()
)
client = TestClient(app)

MINE = "u-alice"
THEIRS = "u-bob"
# A user that was never created, and a string that is not a plausible id at all. Both
# are "an identifier that names nothing" in the only sense Trace has one.
INVENTED = "u-never-existed-00000000"
NOT_AN_ID = "../../etc/passwd"


@pytest.fixture(autouse=True)
def _fake_profile_no_rate_limit(monkeypatch):
    monkeypatch.setenv("NEULIT_PROFILE", "fake")
    get_services.cache_clear()
    # 5/minute on /memory/forget would make this file a study of 429s.
    was_enabled = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was_enabled
    get_services.cache_clear()


@pytest.fixture
def memory():
    """The store itself, which is the oracle. Not an API call."""
    return get_services().memory


@pytest.fixture
def two_users(memory):
    """One user with data to protect, one to make requests as."""
    memory.set_specialty(MINE, "neuro-oncology")
    memory.record_query(MINE, "s-mine", "a query only alice ran", ["11111111"])
    memory.set_specialty(THEIRS, "cardiology")
    return memory


# ── the finding itself ─────────────────────────────────────────────────────────

def test_the_contract_declares_no_authentication_of_any_kind():
    """If this fails, someone added auth and every test below needs rewriting."""
    assert "securitySchemes" not in SPEC.get("components", {}), \
        "a security scheme appeared; the scoping tests below are no longer the whole " \
        "authorization story and real per-principal tests are now required"
    assert "security" not in SPEC, SPEC.get("security")
    declared = {f"{m.upper()} {p}": op.get("security")
                for p, ops in SPEC["paths"].items() for m, op in ops.items()
                if op.get("security")}
    assert declared == {}, declared


def test_no_route_takes_a_credential_and_user_id_is_caller_asserted():
    """`user_id` is a parameter, not a claim. Nothing else on any route could be one.

    The point is not that a credential is missing from one route; it is that there is
    nothing a caller could send that would make the service treat it as one principal
    rather than another.
    """
    credentialish = {"authorization", "x-api-key", "api_key", "apikey", "token",
                     "bearer", "cookie", "x-user-token"}
    found, user_id_params = [], []
    for path, ops in SPEC["paths"].items():
        for method, op in ops.items():
            for param in op.get("parameters", []):
                name = param["name"].lower()
                if name in credentialish:
                    found.append(f"{method.upper()} {path}: {param['name']}")
                if name == "user_id":
                    user_id_params.append(f"{method.upper()} {path}")
    assert found == [], found
    assert user_id_params, "user_id is the identifier under test; it must appear somewhere"


@pytest.mark.parametrize("call", [
    pytest.param(lambda c: c.post("/memory/specialty",
                                  json={"user_id": MINE, "specialty": "seized"}),
                 id="write-specialty"),
    pytest.param(lambda c: c.post("/memory/forget", json={"user_id": MINE}),
                 id="destroy-memory"),
    pytest.param(lambda c: c.get("/memory/profile", params={"user_id": MINE}),
                 id="read-profile"),
])
def test_another_users_identifier_is_granted_and_not_refused(two_users, call):
    """The finding, stated as an assertion so it cannot quietly stop being true.

    C03 asks that presenting another principal's identifier prove reads reach nothing
    and writes change nothing. In Trace they reach and change everything, because the
    identifier is the whole of the claim. This test asserts the grant, so that adding
    authentication turns it red and forces the file to be rewritten rather than letting
    a real access-control model land beside tests that never noticed.
    """
    r = call(client)
    assert r.status_code in (200, 204), r.text


def test_forget_addressed_at_another_user_really_destroys_their_memory(two_users, memory):
    """Not merely a 204: checked in the store, which the request never passed through."""
    assert memory.get_profile(MINE).specialty == "neuro-oncology"
    assert client.post("/memory/forget", json={"user_id": MINE}).status_code == 204
    assert memory.get_profile(MINE).specialty is None, \
        "the 204 was cosmetic; use a different assertion for the finding"


# ── scoping: the boundary that does exist ──────────────────────────────────────

def test_a_read_naming_one_user_returns_nothing_belonging_to_the_other(two_users):
    r = client.get("/memory/profile", params={"user_id": THEIRS})
    assert r.status_code == 200
    body = r.text
    assert "neuro-oncology" not in body, f"alice's specialty leaked into bob's profile: {body}"
    assert MINE not in body, body
    assert r.json()["specialty"] == "cardiology"


def test_a_thread_read_naming_one_session_returns_nothing_from_another(two_users):
    r = client.get("/memory/thread", params={"user_id": THEIRS, "session_id": "s-mine"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["queries"] == [], payload
    assert payload["pmids_shown"] == [], payload


@pytest.mark.parametrize("call,label", [
    (lambda c, uid: c.post("/memory/specialty", json={"user_id": uid, "specialty": "x"}),
     "specialty"),
    (lambda c, uid: c.post("/memory/forget", json={"user_id": uid}), "forget"),
])
def test_a_write_naming_one_user_changes_nothing_of_the_others(two_users, memory, call, label):
    """The oracle is the stored profile, read from the port and not from the API."""
    before = copy.deepcopy(memory.get_profile(MINE).__dict__)

    assert call(client, THEIRS).status_code == 204
    after = memory.get_profile(MINE).__dict__
    assert after == before, f"a {label} write scoped to {THEIRS} mutated {MINE}: {after}"


def test_a_write_naming_an_invented_user_creates_nothing_for_anyone_else(two_users, memory):
    before = copy.deepcopy(memory.get_profile(MINE).__dict__)
    for uid in (INVENTED, NOT_AN_ID):
        assert client.post("/memory/specialty",
                           json={"user_id": uid, "specialty": "ghost"}).status_code == 204
    assert memory.get_profile(MINE).__dict__ == before
    assert memory.get_profile(THEIRS).specialty == "cardiology"


# ── the refusal must not be an oracle for which ids are real ───────────────────

@pytest.mark.parametrize("path,params", [
    ("/memory/profile", lambda uid: {"user_id": uid}),
    ("/memory/thread", lambda uid: {"user_id": uid, "session_id": "s-none"}),
])
def test_two_kinds_of_unknown_identifier_answer_identically(path, params):
    """A well-formed id that names nobody and a string that is not an id at all.

    Trace answers both with an empty record rather than an error, so the two are
    identical apart from the id each request echoed back. Substituting one id for the
    other has to make the two bodies equal; if it did not, the difference would be
    telling a caller which of its guesses had the right shape.
    """
    a = client.get(path, params=params(INVENTED))
    b = client.get(path, params=params(NOT_AN_ID))
    assert a.status_code == b.status_code == 200, (a.status_code, b.status_code)
    assert a.text.replace(json.dumps(INVENTED)[1:-1], "ID") == \
           b.text.replace(json.dumps(NOT_AN_ID)[1:-1], "ID"), (a.text, b.text)


def test_a_real_users_record_is_distinguishable_from_an_invented_one(two_users):
    """The other half of the oracle, and it is NOT closed. This is the finding.

    C03 asks that a refusal be byte-identical for a real identifier belonging to
    another principal and for an invented one, so the error cannot be used to discover
    which objects exist. Trace cannot satisfy that and no error shaping would fix it:
    with no authenticated principal there is no such thing as "another principal's
    record", so every real record is served in full to anyone who names it. Existence
    is disclosed by the successful response, not by an error.

    Asserted rather than omitted, so the gap is a recorded fact that fails if the
    situation changes. This is the point at which an identity model would be required.
    """
    real = client.get("/memory/profile", params={"user_id": MINE})
    invented = client.get("/memory/profile", params={"user_id": INVENTED})
    assert real.status_code == invented.status_code == 200
    assert real.json()["specialty"] == "neuro-oncology"
    assert invented.json()["specialty"] is None
