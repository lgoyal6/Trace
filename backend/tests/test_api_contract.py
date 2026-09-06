"""Contract tests for the HTTP surface: does the app behave the way
frontend/.openapi.json says it does, and does it stay a documented status code
when the request is malformed?

Everything runs against the `fake` profile - in-process fakes seeded from
backend/data/corpus.json, no Snowflake account and no EverOS key - so the
malformed and destructive requests below reach nothing real.

The spec is not regenerated here. `frontend/src/lib/api-types.ts` is generated
from `frontend/.openapi.json` by `npm run types:gen`, so the check that
matters is that the committed spec still matches the app: a shape change that
was not regenerated fails here rather than in the browser.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.limiter import limiter
from backend.api.main import app
from backend.contracts.registry import get_services

client = TestClient(app)
SPEC_PATH = Path(__file__).resolve().parent.parent.parent / "frontend" / ".openapi.json"
COMMITTED_SPEC = json.loads(SPEC_PATH.read_text())

DEMO_QUERY = "localized hypermetabolic uptake pattern on brain imaging"
DOCUMENTED_STATUSES = {200, 204, 400, 404, 405, 415, 422, 429}


@pytest.fixture(autouse=True)
def _fake_profile_no_rate_limit(monkeypatch):
    monkeypatch.setenv("NEULIT_PROFILE", "fake")
    get_services.cache_clear()
    # The limits are real behaviour and stay on for the rest of the suite; this
    # module deliberately sends many requests to one route.
    was_enabled = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was_enabled
    get_services.cache_clear()


# -- a validator for the subset of JSON Schema that FastAPI emits -------------

def _resolve(schema: dict, spec: dict) -> dict:
    while "$ref" in schema:
        ref = schema["$ref"].removeprefix("#/")
        node = spec
        for part in ref.split("/"):
            node = node[part]
        schema = node
    return schema


def validate(value, schema: dict, spec: dict, path: str = "$") -> list[str]:
    """Returns a list of contract violations. Covers $ref, type, properties,
    required, items, anyOf, allOf, enum and additionalProperties - which is
    everything pydantic/FastAPI put in this spec (asserted below)."""
    schema = _resolve(schema, spec)
    errors: list[str] = []
    if not schema:
        return errors

    if "anyOf" in schema:
        if not any(not validate(value, s, spec, path) for s in schema["anyOf"]):
            errors.append(f"{path}: matches none of anyOf")
        return errors
    for sub in schema.get("allOf", []):
        errors += validate(value, sub, spec, path)

    types = schema.get("type")
    if isinstance(types, str):
        types = [types]
    if types:
        checks = {
            "object": dict, "array": list, "string": str,
            "integer": int, "number": (int, float), "boolean": bool, "null": type(None),
        }
        if not any(_is_type(value, t, checks) for t in types if t in checks):
            errors.append(f"{path}: expected {types}, got {type(value).__name__}")
            return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']}")

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name}: required but missing")
        props = schema.get("properties", {})
        for name, sub_value in value.items():
            if name in props:
                errors += validate(sub_value, props[name], spec, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{name}: not in the documented properties")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate(item, schema["items"], spec, f"{path}[{i}]")
    return errors


def _is_type(value, name: str, checks: dict) -> bool:
    """`True` is an `int` in Python but not an integer in JSON Schema, so bool
    is matched only by "boolean"."""
    if name == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    return isinstance(value, checks[name])


def _response_schema(path: str, method: str) -> dict | None:
    op = COMMITTED_SPEC["paths"][path][method]
    content = op.get("responses", {}).get("200", {}).get("content", {})
    return content.get("application/json", {}).get("schema")


# -- the spec itself ----------------------------------------------------------

def test_committed_openapi_spec_matches_the_app():
    """api-types.ts is generated from this file. If it drifts, the generated
    client is describing an API that no longer exists."""
    live = app.openapi()
    assert live["paths"].keys() == COMMITTED_SPEC["paths"].keys()
    assert live["components"]["schemas"] == COMMITTED_SPEC["components"]["schemas"]
    assert live["paths"] == COMMITTED_SPEC["paths"]


def test_the_validator_covers_every_keyword_the_spec_uses():
    """Guards the validator above: a spec keyword it does not implement would
    make these tests pass by ignoring the constraint."""
    supported = {
        "$ref", "type", "properties", "required", "items", "anyOf", "allOf",
        "enum", "additionalProperties", "title", "description", "default",
        "format", "const", "examples", "maxLength", "minLength", "prefixItems",
    }
    seen: set[str] = set()

    def walk(node, *, is_schema: bool):
        """`properties` and the schema registry are name -> schema maps, so
        their keys are field names rather than JSON Schema keywords."""
        if isinstance(node, dict):
            if is_schema:
                seen.update(node.keys())
            for key, value in node.items():
                walk(value, is_schema=not (is_schema and key == "properties"))
        elif isinstance(node, list):
            for value in node:
                walk(value, is_schema=is_schema)

    for schema in COMMITTED_SPEC["components"]["schemas"].values():
        walk(schema, is_schema=True)
    unsupported = seen - supported
    assert not unsupported, f"spec uses keywords the validator ignores: {sorted(unsupported)}"


# -- actual responses against the documented shape ----------------------------

def test_query_response_validates_against_the_documented_schema():
    response = client.post("/query", json={
        "query": DEMO_QUERY, "session_id": "s1", "user_id": "u1", "personalize": True,
    })
    assert response.status_code == 200
    errors = validate(response.json(), _response_schema("/query", "post"), COMMITTED_SPEC)
    assert errors == []


@pytest.mark.parametrize("path", ["/conditions", "/demo-contrast", "/health"])
def test_documented_get_responses_validate(path):
    response = client.get(path)
    assert response.status_code == 200
    errors = validate(response.json(), _response_schema(path, "get"), COMMITTED_SPEC)
    assert errors == []


def test_memory_profile_response_validates():
    response = client.get("/memory/profile", params={"user_id": "u-contract"})
    assert response.status_code == 200
    errors = validate(response.json(), _response_schema("/memory/profile", "get"), COMMITTED_SPEC)
    assert errors == []


def test_atlas_query_returns_html():
    """The gap this used to record as known is closed; see
    backend/tests/test_api_schemathesis_regressions.py, which asserts the
    document half as well. Kept here because this file is where a reader looks
    for what the route really answers."""
    response = client.get("/atlas/query", params={"conditions": ""})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


# -- boundary inputs ----------------------------------------------------------

MALFORMED_QUERY_BODIES = [
    pytest.param({}, id="empty-object"),
    pytest.param({"query": None, "session_id": "s", "user_id": "u"}, id="null-query"),
    pytest.param({"query": "", "session_id": "s", "user_id": "u"}, id="empty-query"),
    pytest.param({"query": "x" * 501, "session_id": "s", "user_id": "u"}, id="over-max-length"),
    pytest.param({"query": "x" * 500, "session_id": "s", "user_id": "u"}, id="at-max-length"),
    pytest.param({"query": "q", "session_id": "s", "user_id": "u", "policy": "GENEROUS"}, id="wrong-case-policy"),
    pytest.param({"query": "q", "session_id": "s", "user_id": "u", "policy": "unknown"}, id="unknown-policy"),
    pytest.param({"query": "q", "session_id": "s", "user_id": "u", "policy": 7}, id="numeric-policy"),
    pytest.param({"query": 1, "session_id": "s", "user_id": "u"}, id="numeric-query"),
    pytest.param({"query": ["q"], "session_id": "s", "user_id": "u"}, id="array-query"),
    pytest.param({"query": {"$ne": None}, "session_id": "s", "user_id": "u"}, id="object-query"),
    pytest.param({"query": "q", "session_id": "s", "user_id": "u", "personalize": "yes"}, id="string-bool"),
    pytest.param({"query": "q", "session_id": "s", "user_id": "u", "top_k": 10 ** 9}, id="unknown-field"),
    pytest.param({"query": "q", "session_id": "s" * 10000, "user_id": "u"}, id="huge-session-id"),
    pytest.param({"query": "q \u200b\ufeff\U0001f9e0", "session_id": "s", "user_id": "u"}, id="control-and-astral"),
]


@pytest.mark.parametrize("body", MALFORMED_QUERY_BODIES)
def test_query_boundary_bodies_never_500(body):
    response = client.post("/query", json=body)
    assert response.status_code in DOCUMENTED_STATUSES, response.text
    if response.status_code == 200:
        assert validate(response.json(), _response_schema("/query", "post"), COMMITTED_SPEC) == []


def test_unknown_request_fields_are_ignored_not_honoured():
    """An extra field must not become a knob. `top_k` is a real pipeline
    parameter that the request shape deliberately does not expose."""
    response = client.post("/query", json={
        "query": DEMO_QUERY, "session_id": "s", "user_id": "u",
        "top_k": 1000, "policy_override": "generous", "personalize": False,
    })
    assert response.status_code == 200
    assert response.json()["policy"] is None


@pytest.mark.parametrize("payload,content_type", [
    ("not json at all", "application/json"),
    ('{"query": "q"', "application/json"),
    ("query=q&session_id=s&user_id=u", "application/x-www-form-urlencoded"),
    ("<query>q</query>", "application/xml"),
    ("", "application/json"),
])
def test_wrong_content_types_and_bodies_never_500(payload, content_type):
    response = client.post(
        "/query", content=payload, headers={"Content-Type": content_type}
    )
    assert response.status_code in DOCUMENTED_STATUSES, response.text


@pytest.mark.parametrize("window", ["24h", "1h", "", "0", "-1", "99999h", "24h; DROP TABLE X", "null"])
def test_economics_summary_window_is_bounded(window):
    response = client.get("/economics/summary", params={"window": window})
    assert response.status_code in DOCUMENTED_STATUSES, response.text
    if response.status_code == 200:
        assert validate(
            response.json(), _response_schema("/economics/summary", "get"), COMMITTED_SPEC
        ) == []


@pytest.mark.parametrize("request_id", ["not-a-uuid", "..%2f..%2fetc%2fpasswd", "a" * 5000, "1 OR 1=1"])
def test_economics_request_path_params_never_500(request_id):
    response = client.get(f"/economics/request/{request_id}")
    assert response.status_code in DOCUMENTED_STATUSES, response.text


@pytest.mark.parametrize("params", [
    {}, {"user_id": ""}, {"user_id": "u", "session_id": ""},
    {"user_id": "../../etc/passwd"}, {"user_id": "u" * 5000},
])
def test_memory_reads_with_boundary_identifiers_never_500(params):
    for path in ("/memory/profile", "/memory/thread"):
        response = client.get(path, params=params)
        assert response.status_code in DOCUMENTED_STATUSES, (path, response.text)


# -- ownership ----------------------------------------------------------------

def test_one_users_memory_is_never_returned_to_another():
    client.post("/memory/specialty", json={"user_id": "owner", "specialty": "neurology"})
    client.post("/query", json={
        "query": DEMO_QUERY, "session_id": "s-owner", "user_id": "owner", "personalize": True,
    })

    other = client.get("/memory/profile", params={"user_id": "stranger"}).json()
    assert other["specialty"] is None
    assert other["conditions_explored"] == []
    assert other["query_count"] == 0
    assert other["seen_pmid_count"] == 0


def test_a_session_thread_belongs_to_one_user_and_one_session():
    client.post("/query", json={
        "query": DEMO_QUERY, "session_id": "shared-session", "user_id": "owner2",
        "personalize": True,
    })

    same = client.get(
        "/memory/thread", params={"user_id": "owner2", "session_id": "shared-session"}
    ).json()
    assert same["queries"] == [DEMO_QUERY]

    # same session id, different user
    other_user = client.get(
        "/memory/thread", params={"user_id": "stranger2", "session_id": "shared-session"}
    ).json()
    assert other_user["queries"] == []
    assert other_user["pmids_shown"] == []

    # same user, different session
    other_session = client.get(
        "/memory/thread", params={"user_id": "owner2", "session_id": "other-session"}
    ).json()
    assert other_session["queries"] == []


def test_forgetting_one_user_leaves_another_untouched():
    for user in ("keeper", "goner"):
        client.post("/query", json={
            "query": DEMO_QUERY, "session_id": f"s-{user}", "user_id": user, "personalize": True,
        })

    assert client.post("/memory/forget", json={"user_id": "goner"}).status_code == 204

    goner = client.get("/memory/profile", params={"user_id": "goner"}).json()
    keeper = client.get("/memory/profile", params={"user_id": "keeper"}).json()
    assert goner["query_count"] == 0 and goner["seen_pmid_count"] == 0
    assert keeper["query_count"] >= 1
