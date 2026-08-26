"""Tier 1 - credential-free. CortexSearchRetriever with a mocked Snowpark
session: exclude_pmids applied before truncation, top_k respected, rarity
multipliers land in ScoredPaper, unavailable session returns [] not raises.
"""
from __future__ import annotations

from backend.snowflake import retrieval as retrieval_module
from backend.snowflake.retrieval import CortexSearchRetriever


def _row(pmid, score, is_rare=False, title="T", abstract="A"):
    return {
        "PMID": pmid,
        "TITLE": title,
        "ABSTRACT": abstract,
        "JOURNAL": "J",
        "PUB_YEAR": 2020,
        "CONDITION": "cond",
        "IS_RARE": is_rare,
        "URL": "u",
        "@score": score,
    }


def test_search_returns_empty_when_unavailable(monkeypatch):
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: False)
    r = CortexSearchRetriever()
    assert r.search("query") == []


def test_search_respects_top_k(monkeypatch):
    rows = [_row(str(i), float(i)) for i in range(10)]
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(CortexSearchRetriever, "_raw_search", lambda self, q, limit: rows)
    r = CortexSearchRetriever()
    result = r.search("query", top_k=3)
    assert len(result) == 3


def test_exclude_pmids_applied_before_truncation(monkeypatch):
    # 5 candidates, exclude the top 2 by score; top_k=3 should still return 3
    # results drawn from the remaining candidates, proving exclusion happens
    # before truncation rather than after (which would short the list).
    rows = [_row(str(i), float(i)) for i in range(5)]
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(CortexSearchRetriever, "_raw_search", lambda self, q, limit: rows)
    r = CortexSearchRetriever()
    result = r.search("query", top_k=3, exclude_pmids=["4", "3"])
    assert len(result) == 3
    assert {sp.paper.pmid for sp in result} == {"0", "1", "2"}


def test_rarity_multiplier_lands_in_scored_paper(monkeypatch):
    rows = [_row("rare1", 1.0, is_rare=True), _row("common1", 1.0, is_rare=False)]
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(CortexSearchRetriever, "_raw_search", lambda self, q, limit: rows)
    r = CortexSearchRetriever()
    result = r.search("query", top_k=2, apply_rarity=True)
    by_pmid = {sp.paper.pmid: sp for sp in result}
    assert by_pmid["rare1"].rarity_multiplier > by_pmid["common1"].rarity_multiplier
    assert by_pmid["rare1"].score > by_pmid["common1"].score


def test_apply_rarity_false_yields_multiplier_one(monkeypatch):
    rows = [_row("rare1", 1.0, is_rare=True)]
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(CortexSearchRetriever, "_raw_search", lambda self, q, limit: rows)
    r = CortexSearchRetriever()
    result = r.search("query", top_k=1, apply_rarity=False)
    assert result[0].rarity_multiplier == 1.0


def test_closest_conditions_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: False)
    r = CortexSearchRetriever()
    assert r.closest_conditions("query") == []


def test_get_by_pmids_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: False)
    r = CortexSearchRetriever()
    assert r.get_by_pmids(["1", "2"]) == []


def test_health_unavailable(monkeypatch):
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: False)
    r = CortexSearchRetriever()
    health = r.health()
    assert health["ok"] is False
