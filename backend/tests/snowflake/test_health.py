"""Tier 1 - credential-free. health() shape for all Card 1 ports/wrappers."""
from __future__ import annotations

from backend.snowflake import analyst as analyst_module
from backend.snowflake import ledger as ledger_module
from backend.snowflake import llm as llm_module
from backend.snowflake import retrieval as retrieval_module
from backend.snowflake.analyst import CortexAnalyst
from backend.snowflake.ledger import SnowflakeLedger
from backend.snowflake.llm import CortexLLMClient
from backend.snowflake.retrieval import CortexSearchRetriever


def test_retrieval_health_shape(monkeypatch):
    monkeypatch.setattr(retrieval_module, "snowflake_available", lambda: False)
    health = CortexSearchRetriever().health()
    assert set(health.keys()) == {"ok", "detail"}
    assert health["ok"] is False


def test_llm_health_shape(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: False)
    health = CortexLLMClient().health()
    assert set(health.keys()) == {"ok", "detail"}
    assert health["ok"] is False


def test_ledger_health_shape(monkeypatch):
    monkeypatch.setattr(ledger_module, "snowflake_available", lambda: False)
    l = SnowflakeLedger()
    try:
        health = l.health()
        assert set(health.keys()) == {"ok", "detail", "queued", "dropped", "last_flush_iso"}
        assert health["ok"] is False
    finally:
        l.stop()


def test_analyst_health_shape(monkeypatch):
    monkeypatch.setattr(analyst_module, "snowflake_available", lambda: False)
    health = CortexAnalyst().health()
    assert set(health.keys()) == {"ok", "detail"}
    assert health["ok"] is False
