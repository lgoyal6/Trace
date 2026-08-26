"""Reassigned to Card 1 at contracts-v1. v1's ParitokLLMClient (Groq + Gemini
failover, backend/app/llm_client.py) is deleted. v2 is a single-provider
CortexLLMClient (backend/snowflake/llm.py) against SNOWFLAKE.CORTEX.COMPLETE
- no fallback provider, by design (plan-v2/01-PHASE-CARD-1-snowflake-platform.md
section 3.5, "Do not add a Groq fallback back in 'just in case'").

Kept as this filename per plan-v2/00-SHARED-CONTRACTS.md's "keep filenames"
rule; the exactly-one-ledger-event-per-call() contract behavior itself is
covered in depth by backend/tests/snowflake/test_llm_contract.py. This file
covers the pieces of the contract that read naturally as "the LLM client's
own tests": model selection from env, no-fallback guarantee, degraded
ChatResult shape.
"""
from __future__ import annotations

from backend.contracts.fakes import FakeLedger
from backend.contracts.models import ChatResult, Message
from backend.snowflake import llm as llm_module
from backend.snowflake.llm import CortexLLMClient


def test_model_defaults_to_claude_sonnet_4_5(monkeypatch):
    # claude-3-5-sonnet is unavailable on COMPLETE in this account's Cortex
    # region (confirmed live, see Decisions.md); claude-sonnet-4-5 is the
    # substitute per the documented contingency.
    monkeypatch.delenv("SNOWFLAKE_CORTEX_MODEL", raising=False)
    assert llm_module._active_model() == "claude-sonnet-4-5"


def test_model_reads_env_override(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_CORTEX_MODEL", "some-other-model")
    assert llm_module._active_model() == "some-other-model"


def test_no_fallback_provider_attribute_exists():
    """CortexLLMClient has no secondary/fallback client - a single provider
    is what makes the ledger's cost numbers trustworthy."""
    client = CortexLLMClient()
    assert not hasattr(client, "_gemini_client")
    assert not hasattr(client, "_groq_client")
    assert not hasattr(client, "_proxy_client")


def test_degraded_chat_result_shape_when_snowflake_unavailable(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: False)
    ledger = FakeLedger()
    client = CortexLLMClient(ledger=ledger)

    result = client.chat(
        [Message(role="user", content="hi")],
        call_site="summary",
        request_id="r1",
        session_id="s1",
        user_id="u1",
    )

    assert isinstance(result, ChatResult)
    assert result.degraded is True
    assert result.usage.cost_usd == 0.0
    assert result.usage.total_tokens == 0


def test_health_reports_unavailable_without_credentials(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: False)
    client = CortexLLMClient()
    health = client.health()
    assert health["ok"] is False
