from backend.seed import run_seed_demo


def test_run_seed_demo_returns_a_populated_result(monkeypatch):
    monkeypatch.setenv("NEULIT_PROFILE", "fake")

    result = run_seed_demo(profile="fake")

    assert result["request_id"]
    assert len(result["papers"]) > 0
    assert result["cost"]["total_tokens"] > 0
    assert set(result.keys()) == {
        "request_id", "summary_markdown", "citations", "papers", "trace",
        "region", "memory", "cost",
        # Additive, optional, null unless a retrieval policy was requested --
        # see Decisions.md, "POST /query gains an optional policy".
        "policy",
        # Additive grounding block: per-claim verdicts, the rank-annotated
        # provenance of the record set the answer was built from, and whether
        # the answer abstained because nothing retrieved supported it.
        "grounding", "retrieval_provenance", "abstained",
    }
