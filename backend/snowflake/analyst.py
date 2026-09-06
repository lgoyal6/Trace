"""FROZEN filename at tag contracts-v1. Body owned by Card 1 (Snowflake platform).

Wraps Cortex Analyst for /economics/ask natural-language cost questions, per
plan-v2/00-SHARED-CONTRACTS.md section 0/4 and
plan-v2/01-PHASE-CARD-1-snowflake-platform.md section 3.7.

Cortex Analyst is called over its REST endpoint
(/api/v2/cortex/analyst/message), authenticated with the same session's
token, pointed at the semantic model file at snowflake/sql/semantic_model.yaml
staged in Snowflake. On any failure this returns a clear "unavailable"
answer rather than raising - /economics/ask must never 500.

Two things this module used to get wrong, both fixed here:

  * the call went to `SNOWFLAKE.CORTEX.COMPLETE(model, OBJECT_CONSTRUCT(
    'semantic_model_file', ..., 'messages', ...))`. COMPLETE has no
    `semantic_model_file` argument and no signature taking a bare object as
    its second parameter - it takes a prompt string or a messages array, and
    it is not Analyst. The semantic model only means anything to the Analyst
    REST endpoint the docstring above already named, so that is what this
    calls, through the connector's own authenticated REST session.

  * the SQL Analyst generated was executed verbatim. That statement is model
    output, reached from an unauthenticated `POST /economics/ask` body, so it
    is untrusted: `_read_only_statement` refuses anything that is not a single
    SELECT/WITH. The durable control is a read-only role for the warehouse
    this runs on; this check is what stands in front of it.
"""
from __future__ import annotations

import logging
import os
import re

from backend.snowflake.session import get_session, snowflake_available

logger = logging.getLogger("neulit.snowflake.analyst")

_SEMANTIC_MODEL_STAGE = os.environ.get(
    "SNOWFLAKE_SEMANTIC_MODEL_STAGE",
    "@NEULIT.CORE.SEMANTIC_MODELS/semantic_model.yaml",
)
_ANALYST_PATH = "/api/v2/cortex/analyst/message"

_UNAVAILABLE = {
    "answer": "Cortex Analyst is unavailable (no Snowflake connection or the request failed).",
    "sql": "",
    "rows": [],
}

_LITERAL_OR_COMMENT_RE = re.compile(
    r"'(?:''|\\.|[^'])*'"     # single-quoted literal, both escape forms
    r"|\"(?:\"\"|[^\"])*\""   # quoted identifier
    r"|--[^\n]*"              # line comment
    r"|/\*.*?\*/",            # block comment
    re.DOTALL,
)

#: Anything that writes, changes session state, or runs something else.
_FORBIDDEN_KEYWORDS = frozenset({
    "alter", "begin", "call", "commit", "copy", "create", "delete", "describe",
    "drop", "execute", "get", "grant", "insert", "merge", "put", "remove",
    "revoke", "rollback", "set", "truncate", "undrop", "unset", "update", "use",
})


def _read_only_statement(sql: str) -> str | None:
    """Return `sql` if it is one read-only statement, else None.

    Comments and string literals are blanked out first, so neither a keyword
    hidden in a literal nor a `--` comment changes the verdict.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        return None
    skeleton = _LITERAL_OR_COMMENT_RE.sub(" ", statement)
    if ";" in skeleton:
        return None  # more than one statement
    words = set(re.findall(r"[a-zA-Z_]+", skeleton.lower()))
    if words & _FORBIDDEN_KEYWORDS:
        return None
    return statement if skeleton.lstrip().lower().startswith(("select", "with")) else None


class CortexAnalyst:
    def __init__(self, semantic_model_stage: str | None = None) -> None:
        self._semantic_model_stage = semantic_model_stage or _SEMANTIC_MODEL_STAGE

    def _request(self, question: str, session) -> dict | None:
        """One Analyst REST call over the connector's authenticated session.

        `client="rest"` asks for a plain JSON response rather than the
        Snowflake-SQL envelope the connector uses for query results.
        """
        payload = {
            "semantic_model_file": self._semantic_model_stage,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": question}]}
            ],
        }
        response = session.connection.rest.request(
            _ANALYST_PATH, payload, method="post", client="rest"
        )
        return response if isinstance(response, dict) else None

    @staticmethod
    def _unpack(response: dict) -> tuple[str, str]:
        """Analyst answers as a message whose `content` is a list of typed
        parts: a `text` part is the prose answer, a `sql` part carries the
        statement in `statement`."""
        message = response.get("message") or {}
        parts = message.get("content") or []
        answer_parts, sql = [], ""
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                answer_parts.append(str(part["text"]))
            elif part.get("type") == "sql" and part.get("statement"):
                sql = str(part["statement"])
        return "\n".join(answer_parts), sql

    def ask(self, question: str) -> dict:
        """Returns {"answer": str, "sql": str, "rows": list[dict]}.

        Never raises - any failure degrades to _UNAVAILABLE.
        """
        if not snowflake_available():
            logger.warning("CortexAnalyst.ask: Snowflake unavailable")
            return dict(_UNAVAILABLE)

        session = get_session()
        if session is None:
            return dict(_UNAVAILABLE)

        try:
            response = self._request(question, session)
            if response is None:
                return dict(_UNAVAILABLE)

            answer, sql = self._unpack(response)

            rows: list[dict] = []
            if sql:
                safe_sql = _read_only_statement(sql)
                if safe_sql is None:
                    logger.warning(
                        "CortexAnalyst.ask: refusing to run non-read-only generated SQL: %r", sql
                    )
                    return {
                        "answer": answer or "(no answer returned)",
                        "sql": sql,
                        "rows": [],
                    }
                try:
                    row_objs = session.sql(safe_sql).collect()
                    rows = [r.as_dict() for r in row_objs]
                except Exception:
                    logger.exception("CortexAnalyst.ask: generated SQL failed to execute")

            return {"answer": answer or "(no answer returned)", "sql": sql, "rows": rows}
        except Exception:
            logger.exception("CortexAnalyst.ask failed for question=%r", question)
            return dict(_UNAVAILABLE)

    def health(self) -> dict:
        if not snowflake_available():
            return {"ok": False, "detail": "snowflake unavailable"}
        return {"ok": True, "detail": f"semantic model stage={self._semantic_model_stage}"}
