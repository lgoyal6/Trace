"""POST /query's retrieval-policy parameter.

The contract being pinned: `policy` is opt-in, an unknown label is a 422 rather
than a silent fallback, and a request that asks for a policy gets back proof of
what actually ran.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.app.retrieval.policy import GENEROUS, TIGHT


@pytest.fixture(autouse=True)
def _no_rate_limit():
    """/query is capped at 10/minute. This module deliberately issues more than
    that comparing arms, so the limiter is off here -- its own behaviour is
    covered in test_api_conditions.py, not re-tested through this file."""
    from backend.api.limiter import limiter

    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = True


@pytest.fixture
def client():
    return TestClient(app)


def _body(**over):
    return {"query": "corticobasal syndrome frontoparietal cortex PET",
            "session_id": "s1", "user_id": "u1", **over}


class TestPolicyParameter:
    def test_omitted_policy_keeps_todays_behaviour(self, client):
        r = client.post("/query", json=_body())
        assert r.status_code == 200
        # Null, not a defaulted-in policy: existing callers see no change.
        assert r.json()["policy"] is None

    def test_null_policy_is_accepted(self, client):
        r = client.post("/query", json=_body(policy=None))
        assert r.status_code == 200
        assert r.json()["policy"] is None

    @pytest.mark.parametrize("label", ["tight", "generous"])
    def test_known_policies_are_accepted_and_reported(self, client, label):
        r = client.post("/query", json=_body(policy=label))
        assert r.status_code == 200
        policy = r.json()["policy"]
        assert policy is not None
        assert policy["label"] == label

    def test_reported_dials_match_the_policy_definition(self, client):
        r = client.post("/query", json=_body(policy="generous"))
        policy = r.json()["policy"]
        assert policy["topK"] == GENEROUS.top_k
        assert policy["compressTopN"] == GENEROUS.compress_top_n

    def test_unknown_policy_is_422_not_a_silent_fallback(self, client):
        r = client.post("/query", json=_body(policy="genrous"))
        assert r.status_code == 422
        # The refusal has to name the labels that would have worked. It used to carry
        # policy_for_label's own "unknown retrieval policy ..." text, raised from a
        # field_validator; the labels now live in the annotation so that they also
        # reach the published contract, and pydantic writes the message. Asserting on
        # the two labels rather than on either wording is what makes this test about
        # the behaviour and not about which layer produced it.
        assert "tight" in r.text and "generous" in r.text, r.text

    def test_empty_policy_string_is_rejected(self, client):
        assert client.post("/query", json=_body(policy="")).status_code == 422


class TestPolicyEvidence:
    """A policy claim in the response has to be backed by measured numbers,
    not just an echoed label."""

    def test_compression_is_actually_applied(self, client):
        r = client.post("/query", json=_body(policy="generous"))
        p = r.json()["policy"]
        assert p["promptTokensAfterCompression"] < p["promptTokensBeforeCompression"]
        assert p["tokensSaved"] > 0
        assert 0 < p["reductionPct"] < 100

    def test_tokens_saved_is_internally_consistent(self, client):
        p = client.post("/query", json=_body(policy="tight")).json()["policy"]
        assert p["tokensSaved"] == (
            p["promptTokensBeforeCompression"] - p["promptTokensAfterCompression"]
        )

    def test_generous_puts_more_papers_in_the_prompt_than_tight(self, client):
        tight = client.post("/query", json=_body(policy="tight")).json()["policy"]
        generous = client.post("/query", json=_body(policy="generous")).json()["policy"]
        assert generous["papersInPrompt"] > tight["papersInPrompt"]

    def test_generous_compresses_harder_per_paper(self, client):
        tight = client.post("/query", json=_body(policy="tight")).json()["policy"]
        generous = client.post("/query", json=_body(policy="generous")).json()["policy"]
        assert generous["reductionPct"] > tight["reductionPct"]

    def test_response_papers_stay_uncompressed(self, client):
        """Compression feeds the summary prompt only. The abstracts the user
        reads -- and that check_citations verifies against -- must be whole."""
        plain = client.post("/query", json=_body()).json()
        generous = client.post("/query", json=_body(policy="generous")).json()

        by_pmid = {p["paper"]["pmid"]: p["paper"]["abstract"] for p in plain["papers"]}
        overlap = 0
        for sp in generous["papers"]:
            pmid = sp["paper"]["pmid"]
            if pmid in by_pmid:
                overlap += 1
                assert sp["paper"]["abstract"] == by_pmid[pmid]
        assert overlap > 0, "no shared papers between arms; test proved nothing"


class TestPolicyOnStream:
    def test_stream_accepts_policy_and_reports_it(self, client):
        with client.stream("POST", "/query/stream", json=_body(policy="generous")) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert '"generous"' in body

    def test_stream_rejects_unknown_policy(self, client):
        r = client.post("/query/stream", json=_body(policy="nope"))
        assert r.status_code == 422
