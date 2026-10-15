"""Integrity checks for the retained local S9 retrieval study."""
from backend.measurement.run_s9_eval import evaluate, rrf
from backend.measurement.run_s9_minilm_eval import MODEL_ID, MODEL_REVISION, PROTOCOL as MINILM_PROTOCOL
from backend.contracts.models import Paper


def _paper(pmid: str) -> Paper:
    return Paper(pmid=pmid, title=pmid, abstract="", journal="", year=0,
                 condition="test", is_rare=False, url="")


def test_rrf_is_paired_and_fuses_by_document_rank():
    a, b, c = _paper("a"), _paper("b"), _paper("c")
    fused = rrf([(a, 2.0), (b, 1.0)], [(b, 3.0), (c, 1.0)], k=0, limit=3)
    assert [paper.pmid for paper, _ in fused] == ["b", "a", "c"]


def test_study_has_disjoint_document_folds_and_per_query_raw_results():
    result = evaluate(repetitions=1)
    assert result["fold"]["shared_documents"] == 0
    assert result["fold"]["indexed_unique_documents"] == result["fold"]["test_documents"]
    assert result["fold"]["queries"] > 0
    assert set(result["summary"]) == {"bm25", "dense", "hybrid_rrf"}
    assert len(result["per_query"]) == result["fold"]["queries"]
    assert all(set(row["methods"]) == set(result["summary"]) for row in result["per_query"])


def test_synthetic_no_answer_controls_are_separate_and_gate_cannot_hide_failures():
    result = evaluate(repetitions=1)
    gate = result["fail_closed_control_gate"]
    assert len(result["no_answer_controls"]) == 3
    assert all(row["kind"] == "synthetic_no_answer" for row in result["no_answer_controls"])
    actual_false_accepts = [
        (row["query"], method)
        for row in result["no_answer_controls"]
        for method, metrics in row["methods"].items()
        if metrics["resolver_would_answer"]
    ]
    assert gate["count"] == len(actual_false_accepts)
    assert (gate["status"] == "PASS") == (not actual_false_accepts)
    assert gate["status"] == "PASS"
    assert all(result["abstained"] for row in result["no_answer_controls"]
               for result in row["methods"].values())
    assert all(method["answerable_queries"] == result["fold"]["queries"]
               for method in result["summary"].values())


def test_minilm_protocol_pins_model_and_keeps_feature_hash_as_a_separate_arm():
    assert MODEL_ID == "sentence-transformers/all-MiniLM-L6-v2"
    assert MODEL_REVISION == "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    assert "feature_hash_dense" in MINILM_PROTOCOL["arms"]
    assert "minilm_dense" in MINILM_PROTOCOL["arms"]
    assert "bm25_minilm_rrf" in MINILM_PROTOCOL["arms"]
    assert "feature hashing" in MINILM_PROTOCOL["arms"]["feature_hash_dense"]
