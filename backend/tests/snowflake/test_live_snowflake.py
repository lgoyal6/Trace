"""Tier 2 - live. Phase card section 3.10: real search-service round-trip,
one real COMPLETE call, one real ledger insert+read-back, one real Analyst
question.

Every test here is `@pytest.mark.live` and is skipped automatically by
conftest.py's `pytest_collection_modifyitems` unless SNOWFLAKE_ACCOUNT /
SNOWFLAKE_USER / SNOWFLAKE_PASSWORD are all set in the environment (see
backend/snowflake/session.py's `_connection_params`). None of these were run
in this sandbox - no Snowflake account or credentials were available. Run by
hand once you have a real account:

    SNOWFLAKE_ACCOUNT=... SNOWFLAKE_USER=... SNOWFLAKE_PASSWORD=... \\
        pytest -m live backend/tests/snowflake/test_live_snowflake.py -v

These tests intentionally talk to the real client classes
(CortexSearchRetriever, CortexLLMClient, SnowflakeLedger, CortexAnalyst) with
no mocking of get_session()/snowflake_available() - that's what makes them
Tier 2 rather than a duplicate of the Tier 1 contract tests.
"""
from __future__ import annotations

import time
import uuid

import pytest

from backend.contracts.models import CallSite, Message
from backend.snowflake.analyst import CortexAnalyst
from backend.snowflake.ledger import SnowflakeLedger
from backend.snowflake.llm import CortexLLMClient
from backend.snowflake.retrieval import CortexSearchRetriever
from backend.snowflake.session import snowflake_available

# CP1 gold-set query from plan-v2/01-PHASE-CARD-1-snowflake-platform.md section 4.
GOLD_SET_QUERY = "asymmetric parietal hypometabolism on FDG-PET with progressive apraxia"

# One of the 5 verified queries from snowflake/sql/semantic_model.yaml's
# verified_queries block (Cortex Analyst / /economics/ask).
ANALYST_QUESTION = "What was the total cost in USD over the last 7 days?"


@pytest.mark.live
def test_search_service_round_trip_returns_gold_set_results():
    """Real Cortex Search Service round-trip for the CP1 gold-set query.

    Requires 03_search_service.sql to have been run and PAPERS_SEARCH to be
    ACTIVE (poll `SHOW CORTEX SEARCH SERVICES IN SCHEMA NEULIT.CORE;` first
    if this fails with an empty result - indexing is not instant, see phase
    card section 5 item 1).
    """
    assert snowflake_available(), "SNOWFLAKE_* env vars set but session unavailable - check credentials"

    retriever = CortexSearchRetriever()
    results = retriever.search(GOLD_SET_QUERY, top_k=10)

    assert isinstance(results, list)
    assert len(results) > 0, "gold-set query returned zero results - check PAPERS_SEARCH is ACTIVE and PAPERS is loaded"
    top = results[0]
    assert top.paper.pmid
    assert 0.0 <= top.score
    assert top.rarity_multiplier >= 1.0

    # Paste the sample result into Handoff-Log.md's CP1 hand-off note per
    # plan-v2/01-PHASE-CARD-1-snowflake-platform.md section 4 when this is
    # actually run.


@pytest.mark.live
def test_real_complete_call_returns_ungraded_content():
    """One real SNOWFLAKE.CORTEX.COMPLETE call through CortexLLMClient,
    with a real SnowflakeLedger receiving the resulting LedgerEvent.
    """
    assert snowflake_available(), "SNOWFLAKE_* env vars set but session unavailable - check credentials"

    ledger = SnowflakeLedger()
    try:
        client = CortexLLMClient(ledger=ledger)
        result = client.chat(
            [Message(role="user", content="Reply with exactly the single word: pong")],
            call_site="summary",
            request_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
            user_id="live-test-user",
            max_output_tokens=32,
        )

        assert result.degraded is False, f"chat() degraded unexpectedly: {result.error}"
        assert result.content
        assert result.usage.model
        assert result.usage.prompt_tokens > 0
        assert result.usage.completion_tokens > 0
    finally:
        ledger.stop()


@pytest.mark.live
def test_real_ledger_insert_and_read_back():
    """One real ledger insert (via record() -> background flush -> INSERT)
    and read-back via a direct SELECT against NEULIT.CORE.TOKEN_LEDGER.
    """
    assert snowflake_available(), "SNOWFLAKE_* env vars set but session unavailable - check credentials"

    from backend.contracts.models import LedgerEvent, TokenUsage
    from backend.snowflake.session import get_session

    ledger = SnowflakeLedger()
    request_id = str(uuid.uuid4())
    try:
        event = LedgerEvent(
            request_id=request_id,
            session_id=str(uuid.uuid4()),
            user_id="live-test-user",
            call_site="summary",
            usage=TokenUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="claude-3-5-sonnet",
                cost_usd=0.0001,
            ),
            latency_ms=42,
            degraded=False,
            occurred_at_iso="2026-01-01T00:00:00+00:00",
        )
        ledger.record(event)
        # Force the buffered event out to Snowflake without waiting for the
        # 2-second background flush interval.
        ledger.stop()

        session = get_session()
        assert session is not None
        rows = session.sql(
            "SELECT REQUEST_ID, MODEL, TOTAL_TOKENS FROM NEULIT.CORE.TOKEN_LEDGER "
            "WHERE REQUEST_ID = ?",
            params=[request_id],
        ).collect()
        assert len(rows) == 1, f"expected exactly one read-back row for request_id={request_id}, got {len(rows)}"
        assert rows[0]["MODEL"] == "claude-3-5-sonnet"
        assert rows[0]["TOTAL_TOKENS"] == 15
    finally:
        # stop() is idempotent; safe to call again if the try block raised
        # before reaching the first ledger.stop() above.
        ledger.stop()


@pytest.mark.live
def test_real_analyst_question():
    """One real Cortex Analyst question, per phase card section 3.10 /
    section 6 ("Cortex Analyst answers all 5 verified queries"). This
    exercises one of the 5; a human with credentials should run all 5 from
    snowflake/sql/semantic_model.yaml's verified_queries block and paste
    pass/fail for each into Handoff-Log.md.
    """
    assert snowflake_available(), "SNOWFLAKE_* env vars set but session unavailable - check credentials"

    analyst = CortexAnalyst()
    result = analyst.ask(ANALYST_QUESTION)

    assert result["answer"]
    assert result["answer"] != "Cortex Analyst is unavailable (no Snowflake connection or the request failed)."
    # sql/rows are allowed to be empty for a purely conversational answer,
    # but if Analyst generated SQL it must have executed without raising
    # (analyst.py swallows execution failures into an empty rows list, so
    # this only checks shape, not that SQL was necessarily generated).
    assert isinstance(result["sql"], str)
    assert isinstance(result["rows"], list)
