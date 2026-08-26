"""Tier 1 - credential-free. Queue flush on count/time threshold, drop-oldest
on overflow, atexit flush. NEULIT_PROFILE=fake, no SNOWFLAKE_* env vars.
"""
from __future__ import annotations

import datetime
import time

from backend.contracts.models import CallSite, LedgerEvent, TokenUsage
from backend.snowflake import ledger as ledger_module
from backend.snowflake.ledger import SnowflakeLedger


def _make_event(i: int) -> LedgerEvent:
    return LedgerEvent(
        request_id=f"req-{i}",
        session_id="sess",
        user_id="user",
        call_site="hyde",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2, model="m", cost_usd=0.0),
        latency_ms=1,
        degraded=False,
        occurred_at_iso=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def test_record_is_non_blocking_and_buffers(monkeypatch):
    monkeypatch.setattr(ledger_module, "snowflake_available", lambda: False)
    l = SnowflakeLedger()
    try:
        l.record(_make_event(1))
        health = l.health()
        assert health["queued"] == 1
        assert health["dropped"] == 0
    finally:
        l.stop()


def test_drop_oldest_on_overflow(monkeypatch):
    monkeypatch.setattr(ledger_module, "snowflake_available", lambda: False)
    l = SnowflakeLedger()
    try:
        for i in range(1005):
            l.record(_make_event(i))
        health = l.health()
        assert health["queued"] <= 1000
        assert health["dropped"] >= 5
    finally:
        l.stop()


def test_flush_on_time_threshold_when_snowflake_available(monkeypatch):
    flushed_batches = []

    class FakeSession:
        def sql(self, *args, **kwargs):
            class R:
                def collect(self_inner):
                    flushed_batches.append(True)
                    return []
            return R()

    monkeypatch.setattr(ledger_module, "snowflake_available", lambda: True)
    monkeypatch.setattr(ledger_module, "get_session", lambda: FakeSession())

    l = SnowflakeLedger()
    try:
        l.record(_make_event(1))
        # background thread flushes every 2s; give it a bit more time
        time.sleep(2.5)
        assert len(flushed_batches) >= 1
        health = l.health()
        assert health["last_flush_iso"] is not None
    finally:
        l.stop()


def test_health_shape(monkeypatch):
    monkeypatch.setattr(ledger_module, "snowflake_available", lambda: False)
    l = SnowflakeLedger()
    try:
        health = l.health()
        assert set(health.keys()) == {"ok", "detail", "queued", "dropped", "last_flush_iso"}
    finally:
        l.stop()
