"""FROZEN filename at tag contracts-v1. Body owned by Card 1 (Snowflake platform).

SnowflakeLedger implements backend.contracts.ports.LedgerPort, buffering
LedgerEvents in a bounded queue and flushing them to NEULIT.CORE.TOKEN_LEDGER
from a background thread, per
plan-v2/01-PHASE-CARD-1-snowflake-platform.md section 3.6.

record() must never add latency to a caller's request - it only puts onto an
in-memory queue and returns.
"""
from __future__ import annotations

import atexit
import datetime
import logging
import queue
import threading

from backend.contracts.models import LedgerEvent
from backend.snowflake.session import get_session, snowflake_available

logger = logging.getLogger("neulit.snowflake.ledger")

_QUEUE_MAXSIZE = 1000
_FLUSH_INTERVAL_SECONDS = 2.0
_FLUSH_BATCH_SIZE = 25


class SnowflakeLedger:
    def __init__(self) -> None:
        self._queue: queue.Queue[LedgerEvent] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._dropped = 0
        self._lock = threading.Lock()
        self._last_flush_iso: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="snowflake-ledger-flush", daemon=True)
        self._thread.start()
        atexit.register(self._atexit_flush)

    # -- writer side (called from request threads) -------------------------
    def record(self, event: LedgerEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Drop-oldest: pull one off the front, then try again. Losing a
            # ledger row is acceptable; blocking the caller's query is not.
            try:
                self._queue.get_nowait()
                with self._lock:
                    self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                with self._lock:
                    self._dropped += 1

    # -- background flush thread -------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(_FLUSH_INTERVAL_SECONDS)
            self._flush_batch()

    def _drain_batch(self) -> list[LedgerEvent]:
        batch: list[LedgerEvent] = []
        while len(batch) < _FLUSH_BATCH_SIZE:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _flush_batch(self) -> None:
        batch = self._drain_batch()
        if not batch:
            return
        self._write_rows(batch)
        with self._lock:
            self._last_flush_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _write_rows(self, batch: list[LedgerEvent]) -> None:
        if not snowflake_available():
            logger.warning("ledger: snowflake unavailable, dropping %d buffered events", len(batch))
            with self._lock:
                self._dropped += len(batch)
            return
        session = get_session()
        try:
            values_sql = []
            params: list = []
            for event in batch:
                values_sql.append("(?,?,?,?,?,?,?,?,?,?,?)")
                params.extend([
                    event.request_id,
                    event.session_id,
                    event.user_id,
                    event.call_site,
                    event.usage.model,
                    event.usage.prompt_tokens,
                    event.usage.completion_tokens,
                    event.usage.total_tokens,
                    event.usage.cost_usd,
                    event.latency_ms,
                    event.degraded,
                ])
            sql = (
                "INSERT INTO NEULIT.CORE.TOKEN_LEDGER "
                "(REQUEST_ID, SESSION_ID, USER_ID, CALL_SITE, MODEL, PROMPT_TOKENS, "
                "COMPLETION_TOKENS, TOTAL_TOKENS, COST_USD, LATENCY_MS, DEGRADED, OCCURRED_AT) "
                "SELECT REQUEST_ID, SESSION_ID, USER_ID, CALL_SITE, MODEL, PROMPT_TOKENS, "
                "COMPLETION_TOKENS, TOTAL_TOKENS, COST_USD, LATENCY_MS, DEGRADED, CURRENT_TIMESTAMP() "
                "FROM VALUES " + ",".join(values_sql) + " AS T(REQUEST_ID, SESSION_ID, USER_ID, "
                "CALL_SITE, MODEL, PROMPT_TOKENS, COMPLETION_TOKENS, TOTAL_TOKENS, COST_USD, "
                "LATENCY_MS, DEGRADED)"
            )
            session.sql(sql, params=params).collect()
        except Exception:
            logger.exception("ledger: multi-row INSERT of %d events failed", len(batch))
            with self._lock:
                self._dropped += len(batch)

    def _atexit_flush(self) -> None:
        self._stop.set()
        # Flush whatever is left so a demo run's last request still shows up.
        while True:
            batch = self._drain_batch()
            if not batch:
                break
            self._write_rows(batch)

    def stop(self) -> None:
        """Test/shutdown hook: stop the background flush thread after
        flushing whatever remains, without waiting for process atexit.
        Idempotent - safe to call more than once (e.g. once explicitly and
        once again via the atexit handler at process exit).
        """
        self._atexit_flush()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def health(self) -> dict:
        with self._lock:
            dropped = self._dropped
            last_flush_iso = self._last_flush_iso
        ok = snowflake_available()
        return {
            "ok": ok,
            "detail": "snowflake ledger" if ok else "snowflake unavailable, buffering locally",
            "queued": self._queue.qsize(),
            "dropped": dropped,
            "last_flush_iso": last_flush_iso,
        }
