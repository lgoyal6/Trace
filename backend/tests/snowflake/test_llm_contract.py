"""Tier 1 - credential-free. Mocked COMPLETE: exactly one ledger event per
chat() call across success / retry-then-success / total-failure paths;
json_schema passed through; 20s timeout constant present.
"""
from __future__ import annotations

import json

from backend.contracts.fakes import FakeLedger
from backend.contracts.models import Message
from backend.snowflake import llm as llm_module
from backend.snowflake.llm import CortexLLMClient


def _messages():
    return [Message(role="user", content="hello")]


def test_success_writes_exactly_one_ledger_event(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(
        CortexLLMClient,
        "_call_complete",
        lambda self, payload, model, options: json.dumps(
            {"choices": [{"content": "hi there"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
        ),
    )
    monkeypatch.setattr(CortexLLMClient, "_load_pricing", lambda self: {})

    ledger = FakeLedger()
    client = CortexLLMClient(ledger=ledger)
    result = client.chat(_messages(), call_site="hyde", request_id="r1", session_id="s1", user_id="u1")

    assert result.degraded is False
    assert len(ledger.events) == 1
    assert ledger.events[0].call_site == "hyde"


def test_retry_then_success_writes_exactly_one_ledger_event(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def flaky_complete(self, payload, model, options):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient snowflake error")
        return json.dumps({"choices": [{"content": "ok"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr(CortexLLMClient, "_call_complete", flaky_complete)
    monkeypatch.setattr(CortexLLMClient, "_load_pricing", lambda self: {})

    ledger = FakeLedger()
    client = CortexLLMClient(ledger=ledger)
    result = client.chat(_messages(), call_site="refine", request_id="r2", session_id="s1", user_id="u1")

    assert result.degraded is False
    assert calls["n"] == 2
    assert len(ledger.events) == 1


def test_total_failure_writes_exactly_one_degraded_ledger_event(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        CortexLLMClient,
        "_call_complete",
        lambda self, payload, model, options: (_ for _ in ()).throw(RuntimeError("down")),
    )
    monkeypatch.setattr(CortexLLMClient, "_load_pricing", lambda self: {})

    ledger = FakeLedger()
    client = CortexLLMClient(ledger=ledger)
    result = client.chat(_messages(), call_site="summary", request_id="r3", session_id="s1", user_id="u1")

    assert result.degraded is True
    assert len(ledger.events) == 1
    assert ledger.events[0].degraded is True


def test_unavailable_snowflake_writes_exactly_one_degraded_ledger_event(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: False)
    ledger = FakeLedger()
    client = CortexLLMClient(ledger=ledger)
    result = client.chat(_messages(), call_site="summary", request_id="r4", session_id="s1", user_id="u1")

    assert result.degraded is True
    assert len(ledger.events) == 1


def test_json_schema_passed_through_to_options(monkeypatch):
    monkeypatch.setattr(llm_module, "snowflake_available", lambda: True)
    captured = {}

    def capture_complete(self, payload, model, options):
        captured["options"] = options
        return json.dumps({"result": "ok"})

    monkeypatch.setattr(CortexLLMClient, "_call_complete", capture_complete)
    monkeypatch.setattr(CortexLLMClient, "_load_pricing", lambda self: {})

    schema = {"type": "object", "properties": {"result": {"type": "string"}}}
    ledger = FakeLedger()
    client = CortexLLMClient(ledger=ledger)
    client.chat(_messages(), call_site="relevance_check", request_id="r5", session_id="s1", user_id="u1", json_schema=schema)

    assert captured["options"]["response_format"]["schema"] == schema


def test_timeout_constant_is_20_seconds():
    assert llm_module._TIMEOUT_SECONDS == 20
