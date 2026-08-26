"""Reassigned to Card 1 at contracts-v1. v1's HybridRetriever (BM25 + local
vectors) is deleted - hybrid retrieval now lives in Snowflake Cortex Search
Service (backend/snowflake/retrieval.py, covered by
backend/tests/snowflake/test_retrieval_contract.py). This file now exercises
the two pieces that survive locally: the rarity formula, byte-identical to
v1, and the deterministic demo-fixture contrast that both FakeRetrieval and
CortexSearchRetriever's re-rank agree on the direction of.
"""
from backend.app.retrieval.rarity import COMMON_BOOST, RARE_BOOST, rarity_boost
from backend.app.retrieval.demo_fixture import run_demo_contrast


def test_rarity_boost_favors_rare():
    assert rarity_boost({"rarity": "rare"}) > rarity_boost({"rarity": "common"})


def test_rarity_boost_formula_exact_values():
    assert rarity_boost({"rarity": "rare"}) == RARE_BOOST
    assert rarity_boost({"rarity": "common"}) == COMMON_BOOST
    assert RARE_BOOST == 1.6
    assert COMMON_BOOST == 1.0
    assert rarity_boost({"rarity": "rare"}) / rarity_boost({"rarity": "common"}) == 1.6


def test_rarity_boost_missing_rarity_field_defaults_to_common():
    assert rarity_boost({"pmid": "test"}) == rarity_boost({"rarity": "common"})


def test_demo_contrast_naive_buries_rare_case_but_weighted_surfaces_it():
    result = run_demo_contrast()
    rare_pmid = result["rare_case_pmid"]

    naive_rank = result["naive_top5"].index(rare_pmid) if rare_pmid in result["naive_top5"] else 99
    weighted_rank = result["weighted_top5"].index(rare_pmid) if rare_pmid in result["weighted_top5"] else 99

    assert weighted_rank <= naive_rank
    assert rare_pmid in result["weighted_top5"]
