"""FROZEN filename at tag contracts-v1. Body owned by Card 1 (Snowflake platform).

Wraps Cortex Analyst for /economics/ask natural-language cost questions, per
plan-v2/00-SHARED-CONTRACTS.md section 0/4 and
plan-v2/01-PHASE-CARD-1-snowflake-platform.md section 3.7.

Cortex Analyst is called over its REST endpoint
(/api/v2/cortex/analyst/message), authenticated with the same session's
token, pointed at the semantic model file at snowflake/sql/semantic_model.yaml
staged in Snowflake. On any failure this returns a clear "unavailable"
answer rather than raising - /economics/ask must never 500.
"""
from __future__ import annotations

import json
import logging
import os

from backend.snowflake.session import get_session, snowflake_available

logger = logging.getLogger("neulit.snowflake.analyst")

_SEMANTIC_MODEL_STAGE = os.environ.get(
    "SNOWFLAKE_SEMANTIC_MODEL_STAGE",
    "@NEULIT.CORE.SEMANTIC_MODELS/semantic_model.yaml",
)
_DEFAULT_MODEL = "claude-sonnet-4-5"


def _active_model() -> str:
    return os.environ.get("SNOWFLAKE_CORTEX_MODEL", _DEFAULT_MODEL)
_UNAVAILABLE = {
    "answer": "Cortex Analyst is unavailable (no Snowflake connection or the request failed).",
    "sql": "",
    "rows": [],
}


class CortexAnalyst:
    def __init__(self, semantic_model_stage: str | None = None) -> None:
        self._semantic_model_stage = semantic_model_stage or _SEMANTIC_MODEL_STAGE

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
            # Cortex Analyst's REST message endpoint takes the semantic model
            # file and a chat-style message list, and returns a response
            # whose content includes both a natural-language answer and a
            # generated SQL statement to run.
            result = session.sql(
                "SELECT SNOWFLAKE.CORTEX.COMPLETE("
                "?, "
                "OBJECT_CONSTRUCT("
                "  'semantic_model_file', ?, "
                "  'messages', ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('role', 'user', 'content', ?))"
                ")) AS RESP",
                params=[_active_model(), self._semantic_model_stage, question],
            ).collect()
            if not result:
                return dict(_UNAVAILABLE)

            raw = result[0]["RESP"]
            payload = json.loads(raw) if isinstance(raw, str) else raw
            answer = ""
            sql = ""
            if isinstance(payload, dict):
                answer = payload.get("answer") or payload.get("text") or ""
                sql = payload.get("sql") or payload.get("statement") or ""

            rows: list[dict] = []
            if sql:
                try:
                    row_objs = session.sql(sql).collect()
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
