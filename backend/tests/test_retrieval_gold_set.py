"""Reassigned to Card 1 at contracts-v1. v1 computed condition-centroid
similarity locally with sentence-transformers; v2 moves that embedding step
into Snowflake (SNOWFLAKE.CORTEX.EMBED_TEXT_768 + VECTOR_COSINE_SIMILARITY,
see backend/snowflake/retrieval.py:closest_conditions), which this
credential-free suite cannot call. What stays testable without a live
connection is the pure scoring/threshold math in
backend/app/retrieval/condition_match.py - this file verifies that math
directly, plus the demo-fixture gold case for the flagship rare condition.
"""
import json
from pathlib import Path

from backend.app.retrieval.condition_match import (
    NO_MATCH_THRESHOLD,
    cosine_similarity,
    is_in_scope,
    rank_condition_scores,
)
from backend.app.retrieval.demo_fixture import run_demo_contrast

CORPUS_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus.json"


def test_cosine_similarity_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_cosine_similarity_mismatched_length_is_zero():
    assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0


def test_cosine_similarity_zero_vector_is_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_is_in_scope_respects_threshold():
    assert is_in_scope(NO_MATCH_THRESHOLD) is True
    assert is_in_scope(NO_MATCH_THRESHOLD - 0.01) is False


def test_rank_condition_scores_sorted_descending():
    scores = {"a": 0.1, "b": 0.9, "c": 0.5}
    ranked = rank_condition_scores(scores, top_n=2)
    assert ranked == [("b", 0.9), ("c", 0.5)]


def test_no_match_threshold_is_the_v1_empirical_value():
    assert NO_MATCH_THRESHOLD == 0.42


def test_demo_contrast_flagship_rare_condition_present_in_corpus():
    corpus = json.loads(CORPUS_PATH.read_text())
    scalp_angiosarcoma = [p for p in corpus if p["condition"] == "Scalp angiosarcoma"]
    assert len(scalp_angiosarcoma) >= 3

    result = run_demo_contrast()
    assert result["rare_case_pmid"] == scalp_angiosarcoma[0]["pmid"] or any(
        p["pmid"] == result["rare_case_pmid"] for p in scalp_angiosarcoma
    )
