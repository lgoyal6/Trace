"""Owned by Card 1 (backend/tests/snowflake/* is in the c1 ownership bucket).

Offline tests for backend/snowflake/analyst.py: the request shape it sends,
and what it will and will not run out of what Analyst sends back. No Snowflake
connection is made - a fake session records the REST call. Whether Snowflake
accepts the request is a live question and lives in test_live_snowflake.py.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import backend.snowflake.analyst as analyst_mod
from backend.snowflake.analyst import CortexAnalyst, _read_only_statement


class _FakeRest:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def request(self, url, body=None, method="post", client="sfsql", **kwargs):
        self.calls.append({"url": url, "body": body, "method": method, "client": client})
        return self.response


class _FakeRow:
    def __init__(self, data):
        self._data = data

    def as_dict(self):
        return dict(self._data)


class _FakeSession:
    def __init__(self, response):
        self.connection = type("Conn", (), {"rest": _FakeRest(response)})()
        self.executed: list[str] = []

    def sql(self, text, params=None):
        self.executed.append(text)
        return self

    def collect(self):
        return [_FakeRow({"N": 1})]


def _answer(text="Total cost is $0.12.", sql=""):
    content = [{"type": "text", "text": text}]
    if sql:
        content.append({"type": "sql", "statement": sql})
    return {"message": {"role": "analyst", "content": content}}


def _run(response):
    session = _FakeSession(response)
    with patch.object(analyst_mod, "snowflake_available", lambda: True), \
         patch.object(analyst_mod, "get_session", lambda: session):
        result = CortexAnalyst("@NEULIT.CORE.SEMANTIC_MODELS/semantic_model.yaml").ask(
            "what did the last 24h cost?"
        )
    return result, session


# -- request shape ------------------------------------------------------------

def test_ask_posts_the_analyst_message_endpoint():
    """Analyst is a REST endpoint with a semantic model file, not a COMPLETE
    call - COMPLETE has no `semantic_model_file` argument to pass one to."""
    _, session = _run(_answer())
    call = session.connection.rest.calls[0]
    assert call["url"] == "/api/v2/cortex/analyst/message"
    assert call["method"] == "post"
    assert call["client"] == "rest"
    assert call["body"]["semantic_model_file"] == "@NEULIT.CORE.SEMANTIC_MODELS/semantic_model.yaml"
    assert call["body"]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "what did the last 24h cost?"}]}
    ]


def test_ask_reads_the_text_and_sql_content_parts():
    result, session = _run(_answer(sql="SELECT COUNT(*) FROM NEULIT.CORE.TOKEN_LEDGER"))
    assert result["answer"] == "Total cost is $0.12."
    assert result["sql"] == "SELECT COUNT(*) FROM NEULIT.CORE.TOKEN_LEDGER"
    assert result["rows"] == [{"N": 1}]
    assert session.executed == ["SELECT COUNT(*) FROM NEULIT.CORE.TOKEN_LEDGER"]


def test_ask_degrades_rather_than_raising():
    class Boom:
        rest = type("R", (), {"request": staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))})()

    session = type("S", (), {"connection": Boom()})()
    with patch.object(analyst_mod, "snowflake_available", lambda: True), \
         patch.object(analyst_mod, "get_session", lambda: session):
        result = CortexAnalyst().ask("q")
    assert result["answer"].startswith("Cortex Analyst is unavailable")


# -- generated SQL is model output, not a command ------------------------------

@pytest.mark.parametrize("sql", [
    "DROP TABLE NEULIT.CORE.TOKEN_LEDGER",
    "DELETE FROM NEULIT.CORE.PAPERS",
    "SELECT 1; DROP TABLE NEULIT.CORE.PAPERS",
    "SELECT 1 -- harmless\n; TRUNCATE TABLE NEULIT.CORE.PAPERS",
    "CREATE TABLE X AS SELECT 1",
    "INSERT INTO NEULIT.CORE.PAPERS SELECT * FROM NEULIT.CORE.PAPERS",
    "MERGE INTO NEULIT.CORE.PAPERS USING X ON TRUE WHEN MATCHED THEN DELETE",
    "GRANT OWNERSHIP ON DATABASE NEULIT TO ROLE PUBLIC",
    "CALL SYSTEM$SOMETHING()",
    "COPY INTO @stage FROM NEULIT.CORE.PAPERS",
    "USE ROLE ACCOUNTADMIN",
])
def test_non_read_only_generated_sql_is_not_executed(sql):
    result, session = _run(_answer(sql=sql))
    assert session.executed == [], f"executed refused statement: {session.executed}"
    assert result["rows"] == []
    # still reported back, so the caller can see what Analyst proposed
    assert result["sql"] == sql


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM NEULIT.CORE.TOKEN_LEDGER",
    "select call_site, sum(cost_usd) from NEULIT.CORE.V_COST_BY_CALL_SITE group by 1",
    "WITH h AS (SELECT * FROM NEULIT.CORE.V_COST_BY_HOUR) SELECT * FROM h",
    "SELECT 'drop table x' AS NOTE FROM NEULIT.CORE.PAPERS",
    "SELECT COUNT(*) FROM NEULIT.CORE.PAPERS;",
])
def test_read_only_generated_sql_runs(sql):
    assert _read_only_statement(sql) is not None
    result, session = _run(_answer(sql=sql))
    assert len(session.executed) == 1
    assert result["rows"] == [{"N": 1}]
