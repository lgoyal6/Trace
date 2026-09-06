from fastapi.testclient import TestClient

from backend.api.main import app
from backend.contracts.registry import get_services

client = TestClient(app)

DEMO_QUERY = "localized hypermetabolic uptake pattern on brain imaging"


def _reset_fake_profile(monkeypatch):
    monkeypatch.setenv("NEULIT_PROFILE", "fake")
    get_services.cache_clear()


def test_query_endpoint_returns_the_v2_contract_shape(monkeypatch):
    _reset_fake_profile(monkeypatch)

    response = client.post("/query", json={
        "query": DEMO_QUERY, "session_id": "s1", "user_id": "u1", "personalize": True,
    })

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "request_id", "summary_markdown", "citations", "papers", "trace",
        "region", "memory", "cost",
        # Additive, optional, null unless the request asks for a retrieval
        # policy -- see Decisions.md, "POST /query gains an optional policy".
        "policy",
        # Additive grounding block: per-claim verdicts, the rank-annotated
        # provenance of the record set the answer was built from, and whether
        # the answer abstained because nothing retrieved supported it.
        "grounding", "retrieval_provenance", "abstained",
    }
    assert body["policy"] is None, "policy must stay null when unrequested"
    assert body["request_id"]
    assert isinstance(body["papers"], list) and len(body["papers"]) > 0
    assert set(body["memory"].keys()) == {"applied", "seen_filtered", "profile_used", "distilled_context"}
    assert set(body["cost"].keys()) == {"total_tokens", "cost_usd", "by_call_site"}


def test_query_endpoint_scored_paper_dto_is_camel_case(monkeypatch):
    _reset_fake_profile(monkeypatch)

    response = client.post("/query", json={
        "query": DEMO_QUERY, "session_id": "s1", "user_id": "u1", "personalize": False,
    })

    paper = response.json()["papers"][0]
    assert set(paper.keys()) == {
        "paper", "score", "lexicalScore", "semanticScore", "rarityMultiplier", "memoryMultiplier",
    }
    assert set(paper["paper"].keys()) == {
        "pmid", "title", "abstract", "journal", "year", "condition", "isRare", "url",
    }


def test_query_endpoint_non_personalized_reports_memory_not_applied(monkeypatch):
    _reset_fake_profile(monkeypatch)

    response = client.post("/query", json={
        "query": DEMO_QUERY, "session_id": "s1", "user_id": "u1", "personalize": False,
    })

    assert response.json()["memory"]["applied"] is False


def test_query_endpoint_rejects_empty_query(monkeypatch):
    _reset_fake_profile(monkeypatch)

    response = client.post("/query", json={
        "query": "", "session_id": "s1", "user_id": "u1", "personalize": False,
    })

    assert response.status_code == 422
