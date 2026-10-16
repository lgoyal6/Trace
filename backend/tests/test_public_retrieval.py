import pytest
from demo.live import run_query

BASE = {"query": "Cedar sensor", "top_k": 3, "rarity": False, "claim": ""}


def test_visitor_query_changes_real_retrieval_and_preserves_provenance():
    first = run_query(BASE)
    second = run_query({**BASE, "query": "Harbor queue", "top_k": 2})
    assert first["results"][0]["id"] != second["results"][0]["id"]
    assert len(second["results"]) == 2
    assert all(len(row["content_hash"]) == 64 for row in first["results"])
    assert first == run_query(BASE)


def test_no_overlap_abstains_instead_of_serving_hash_collisions():
    result = run_query({**BASE, "query": "zyxwvu qqqqqq"})
    assert result["status"] == "no-lexical-evidence"
    assert result["results"] == []
    assert not result["grounding"]["answerable"]


def test_grounding_uses_retrieved_text_and_detects_wrong_numbers():
    result = run_query(BASE)
    rank = next(row["citation"] for row in result["results"] if row["id"] == "DEMO-1")
    supported = run_query({**BASE, "claim": f"The Cedar sensor emits 12 samples per second [{rank}]."})
    wrong = run_query({**BASE, "claim": f"The Cedar sensor emits 999 samples per second [{rank}]."})
    assert supported["grounding"]["grounded"] == 1
    assert wrong["grounding"]["unsupported"] == 1


@pytest.mark.parametrize("patch", [{"top_k": True}, {"top_k": 7}, {"query": ""}, {"query": "x" * 257}, {"claim": "x" * 513}, {"rarity": "true"}, {"url": "http://localhost"}])
def test_public_inputs_are_bounded(patch):
    with pytest.raises(ValueError):
        run_query({**BASE, **patch})
