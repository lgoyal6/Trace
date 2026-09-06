"""Regressions for the defects Schemathesis found and the hand-rolled corpus did not.

Each test names the check that produced it, so the provenance of the assertion is
readable from the file. The reproduction in each docstring is the request Schemathesis
shrank to, translated to the in-process client.

Why backend/tests/test_api_contract.py did not find these, and it is not a weak file:
its 33 boundary cases vary *values* inside hand-written request shapes and assert that
the status is one the spec documents. It never reads a response's Content-Type, never
sends a body that is not valid UTF-8 (every case is a Python str, so it cannot), never
checks whether a value the spec permits is actually accepted, and never distinguishes
`0` from `"yes"` when probing a boolean. Those four blind spots are where these live.

`scripts/schemathesis.sh` is the invocation; the harness is
backend/tests/isolated_server.py, which serves this same app over a socket.
"""
from __future__ import annotations

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

BODY = {"query": "brain imaging", "session_id": "s-regress", "user_id": "u-regress"}

# Bytes that are not decodable as UTF-8. `b"\xff\xfe"` is NOT one of these: it decodes
# far enough to reach the JSON parser and lands on 422, which is why picking a
# "malformed" byte string by hand is unreliable and generating one is not.
UNDECODABLE = b"\x10>>\xef\xbf\x56\xef"


@pytest.fixture(autouse=True)
def _fake_profile_no_rate_limit(monkeypatch):
    monkeypatch.setenv("NEULIT_PROFILE", "fake")
    get_services.cache_clear()
    was_enabled = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was_enabled
    get_services.cache_clear()


def _content_types(path, method, status="200"):
    return set(SPEC["paths"][path][method]["responses"][status].get("content", {}))


# ── check: content_type_conformance ────────────────────────────────────────────

@pytest.mark.parametrize("path,url", [
    ("/atlas", "/atlas"),
    ("/atlas/query", "/atlas/query?conditions="),
    ("/atlas/{condition_name}", "/atlas/Scalp%20angiosarcoma"),
])
def test_the_atlas_routes_document_the_html_they_actually_return(path, url):
    """Schemathesis `content_type_conformance` on all three atlas routes.

    Every branch of these routes returns `Response(..., media_type="text/html")`, and
    without a `response_class` FastAPI documented `application/json` with an empty
    schema. openapi-typescript then generated a client that parses HTML as JSON.

    backend/tests/test_api_contract.py had already recorded this in prose - "known gap,
    recorded rather than asserted both ways" - and asserted only the live half. Both
    halves are asserted now, so the document and the response cannot drift apart again.
    """
    assert _content_types(path, "get") == {"text/html"}, _content_types(path, "get")
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html"), r.headers["content-type"]


def test_the_stream_route_documents_server_sent_events_and_not_json():
    """Schemathesis `content_type_conformance` on `POST /query/stream`.

    The route returns `StreamingResponse(media_type="text/event-stream")` and the
    document claimed `application/json`. This is the route the UI actually uses.
    """
    assert _content_types("/query/stream", "post") == {"text/event-stream"}
    with client.stream("POST", "/query/stream", json=BODY) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")


# ── check: status_code_conformance ─────────────────────────────────────────────

@pytest.mark.parametrize("path,url", [
    ("/query", "/query"),
    ("/query/stream", "/query/stream"),
    ("/economics/ask", "/economics/ask"),
    ("/memex/query", "/memex/query"),
    ("/memex/shock", "/memex/shock"),
    ("/memory/specialty", "/memory/specialty"),
    ("/memory/forget", "/memory/forget"),
])
def test_an_undecodable_body_answers_a_status_the_contract_declares(path, url):
    """Schemathesis `status_code_conformance` on every route that takes a body.

    Starlette rejects a body it cannot decode before pydantic runs, so the answer is
    400 and not the 422 that every hand-written malformed body produces. No route
    declared it. The corpus could not have found this: its malformed bodies are Python
    strings, and a Python string is valid UTF-8 by construction.
    """
    r = client.post(url, content=UNDECODABLE,
                    headers={"content-type": "application/json"})
    assert r.status_code == 400, f"{r.status_code}: {r.text[:200]}"
    declared = {int(c) for c in SPEC["paths"][path]["post"]["responses"]}
    assert 400 in declared, f"{path} declares only {sorted(declared)}"


# ── check: negative_data_rejection ─────────────────────────────────────────────

@pytest.mark.parametrize("url,field,extra", [
    ("/query", "personalize", {}),
    ("/query/stream", "personalize", {}),
    ("/memex/query", "personalize", {}),
    ("/memex/query", "settle", {}),
])
@pytest.mark.parametrize("value", [0, 1])
def test_an_integer_is_not_accepted_where_the_contract_declares_a_boolean(url, field, extra, value):
    """Schemathesis `negative_data_rejection` on the three routes with boolean flags.

    Pydantic coerces int to bool outside strict mode, so `{"personalize": 0}` was
    accepted and ran the un-personalized arm, and `{"settle": 0}` booked no trade -
    both with a 200 and both against a contract that declares a boolean.

    The corpus sends `personalize: "yes"` and sees a 422, so the field looked covered.
    A string is not the coercion that fires; `0` is, and no hand-written case used it.
    """
    r = client.post(url, json={**BODY, **extra, field: value})
    assert r.status_code == 422, f"{field}={value} accepted: {r.status_code} {r.text[:200]}"


# ── check: positive_data_acceptance ────────────────────────────────────────────

def test_the_retrieval_policy_labels_are_published_in_the_contract():
    """Schemathesis `positive_data_acceptance` on `POST /query` and `/query/stream`.

    The two legal labels were enforced by a field_validator and absent from the
    document, which declared `policy` as any nullable string. Schemathesis generated
    `policy: ""` - schema-compliant, refused by the service - and reported the API as
    rejecting valid data. A client had no way to discover the labels from the document.
    """
    for path in ("/query", "/query/stream"):
        schema = SPEC["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        ref = schema["$ref"].rsplit("/", 1)[-1]
        policy = SPEC["components"]["schemas"][ref]["properties"]["policy"]
        options = [b for b in policy["anyOf"] if b.get("type") != "null"]
        assert options and options[0].get("enum") == ["tight", "generous"], policy


def test_the_published_labels_are_the_labels_the_policy_module_defines():
    """The enum above is written out in an annotation, so it can drift from the module
    that resolves it. This is the pin: adding a third policy without publishing it
    fails here rather than in a client that cannot discover it.
    """
    from backend.app.retrieval.policy import _BY_LABEL

    from backend.api.schemas import QueryRequest

    declared = QueryRequest.model_fields["policy"].annotation
    published = set(getattr(declared, "__args__")[0].__args__)
    assert published == set(_BY_LABEL), (published, set(_BY_LABEL))


def test_every_frame_the_stream_emits_validates_against_the_documented_event_schema():
    """Schemathesis `response_schema_conformance`, SSE arm, on `POST /query/stream`.

    Declaring `text/event-stream` was only half the fix. FastAPI's default schema for a
    non-JSON response class is `{"type": "string"}`, so the document described the whole
    stream as one string and said nothing a client could use; Schemathesis parses the
    stream and reported every event as violating it. `itemSchema` describes one event
    and `contentSchema` describes the JSON inside its `data:` field, so the union of
    stage/done/error frames is checked frame by frame.

    This test consumes a real stream and validates each frame against the committed
    document, which is what keeps a hand-written JSON Schema from drifting away from
    `event_generator`: adding a fourth event type without publishing it fails here.
    """
    # backend/tests/test_api_contract.py's validator, not a new dependency: it already
    # walks this spec, and test_the_validator_covers_every_keyword_the_spec_uses guards
    # it against a keyword it would silently ignore. The event union is `anyOf` rather
    # than `oneOf` for that reason; the three types are disjoint by their `type` const,
    # so the two mean the same thing here.
    from backend.tests.test_api_contract import validate

    item_schema = SPEC["paths"]["/query/stream"]["post"]["responses"]["200"][
        "content"]["text/event-stream"]["itemSchema"]
    content_schema = item_schema["properties"]["data"]["contentSchema"]

    frames = []
    with client.stream("POST", "/query/stream", json=BODY) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))

    assert frames, "the stream produced no frames"
    assert {f["type"] for f in frames} >= {"stage", "done"}, {f["type"] for f in frames}
    for frame in frames:
        errors = validate(frame, content_schema, SPEC)
        assert errors == [], f"{frame.get('type')} frame: {errors}"
