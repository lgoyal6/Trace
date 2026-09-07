"""LlamaParse page chunks in Snowflake Cortex Search, with deletion and evaluation."""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable

from backend.app.corpus.layout_parse import DocChunk

_COMPONENT = r"[A-Z_][A-Z0-9_$]*"
_IDENTIFIER = re.compile(rf"^{_COMPONENT}(?:\.{_COMPONENT}){{2}}$")
_SIMPLE_IDENTIFIER = re.compile(rf"^{_COMPONENT}$")
DEFAULT_TABLE = "NEULIT.CORE.LAYOUT_CHUNKS"
DEFAULT_SERVICE = "NEULIT.CORE.LAYOUT_SEARCH"
DEFAULT_WAREHOUSE = "NEULIT_WH"


def _identifier(value: str) -> str:
    value = value.upper()
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe Snowflake identifier: {value!r}")
    return value


def _simple_identifier(value: str) -> str:
    value = value.upper()
    if not _SIMPLE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe Snowflake identifier: {value!r}")
    return value


def install(
    session, *, table: str = DEFAULT_TABLE, service: str = DEFAULT_SERVICE,
    warehouse: str = DEFAULT_WAREHOUSE,
) -> None:
    table, service = _identifier(table), _identifier(service)
    warehouse = _simple_identifier(warehouse)
    session.sql(f"""CREATE TABLE IF NOT EXISTS {table} (
      DOC_ID STRING NOT NULL, PAGE NUMBER NOT NULL, BLOCK_INDEX NUMBER NOT NULL,
      KIND STRING NOT NULL, TEXT STRING NOT NULL, PARSER STRING NOT NULL,
      SOURCE_SHA256 STRING NOT NULL, LLAMAPARSE_JOB_ID STRING,
      PRIMARY KEY (DOC_ID, PAGE, BLOCK_INDEX)
    )""").collect()
    session.sql(f"""CREATE CORTEX SEARCH SERVICE IF NOT EXISTS {service}
      ON TEXT ATTRIBUTES DOC_ID, PAGE, BLOCK_INDEX, KIND, PARSER, SOURCE_SHA256
      WAREHOUSE = {warehouse} TARGET_LAG = '1 hour' AUTO_SUSPEND = 60
      AS SELECT TEXT, DOC_ID, PAGE, BLOCK_INDEX, KIND, PARSER, SOURCE_SHA256
      FROM {table}""").collect()
    session.sql(
        f"ALTER CORTEX SEARCH SERVICE {service} SET WAREHOUSE = {warehouse}"
    ).collect()


def index_chunks(
    session, chunks: Iterable[DocChunk], *, table: str = DEFAULT_TABLE,
    service: str = DEFAULT_SERVICE, warehouse: str = DEFAULT_WAREHOUSE,
) -> dict:
    table, service = _identifier(table), _identifier(service)
    rows = list(chunks)
    if not rows:
        return {"documents": 0, "chunks": 0}
    install(session, table=table, service=service, warehouse=warehouse)
    doc_ids = sorted({row.doc_id for row in rows})
    placeholders = ",".join("?" for _ in doc_ids)
    session.sql(
        f"DELETE FROM {table} WHERE DOC_ID IN ({placeholders})", params=doc_ids
    ).collect()
    values = ",".join("(?,?,?,?,?,?,?,?)" for _ in rows)
    params = []
    for row in rows:
        params.extend([
            row.doc_id, row.page, row.block_index, row.kind, row.text, row.parser,
            row.meta.get("source_sha256", ""), row.meta.get("llamaparse_job_id"),
        ])
    session.sql(
        f"INSERT INTO {table} "
        "(DOC_ID,PAGE,BLOCK_INDEX,KIND,TEXT,PARSER,SOURCE_SHA256,LLAMAPARSE_JOB_ID) "
        f"VALUES {values}",
        params=params,
    ).collect()
    session.sql(f"ALTER CORTEX SEARCH SERVICE {service} REFRESH").collect()
    return {"documents": len(doc_ids), "chunks": len(rows)}


def search_chunks(session, query: str, *, top_k: int = 10,
                  service: str = DEFAULT_SERVICE) -> list[dict]:
    service = _identifier(service)
    request = json.dumps({
        "query": query,
        "limit": top_k,
        "columns": [
            "DOC_ID", "PAGE", "BLOCK_INDEX", "KIND", "TEXT", "PARSER",
            "SOURCE_SHA256",
        ],
    })
    rows = session.sql(
        "SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(?, ?) AS RESPONSE",
        params=[service, request],
    ).collect()
    if not rows:
        return []
    raw = rows[0][0]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    return list(payload.get("results", []))


def delete_document(session, doc_id: str, *, table: str = DEFAULT_TABLE,
                    service: str = DEFAULT_SERVICE) -> dict:
    table, service = _identifier(table), _identifier(service)
    session.sql(f"DELETE FROM {table} WHERE DOC_ID = ?", params=[doc_id]).collect()
    session.sql(f"ALTER CORTEX SEARCH SERVICE {service} REFRESH").collect()
    remaining = session.sql(
        f"SELECT COUNT(*) AS N FROM {table} WHERE DOC_ID = ?", params=[doc_id]
    ).collect()
    count = int(remaining[0][0]) if remaining else 0
    if count:
        raise RuntimeError(
            f"document deletion verification failed: {count} chunks remain"
        )
    return {"doc_id": doc_id, "remaining_chunks": 0, "index_refresh_requested": True}


def service_daily_credits(session, *, service: str = DEFAULT_SERVICE) -> float:
    database_name, schema_name, service_name = _identifier(service).split(".")
    rows = session.sql(
        "SELECT COALESCE(SUM(CREDITS), 0) AS CREDITS "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_SEARCH_DAILY_USAGE_HISTORY "
        "WHERE DATABASE_NAME = ? AND SCHEMA_NAME = ? AND SERVICE_NAME = ? "
        "AND USAGE_DATE = CURRENT_DATE()",
        params=[database_name, schema_name, service_name],
    ).collect()
    return float(rows[0][0]) if rows else 0.0


def evaluate_retrieval(session, cases: Iterable[dict], *, top_k: int = 10,
                       service: str = DEFAULT_SERVICE) -> dict:
    cases = list(cases)
    credits_before = service_daily_credits(session, service=service)
    started = time.perf_counter()
    hits = []
    for case in cases:
        results = search_chunks(session, case["query"], top_k=top_k, service=service)
        expected = (str(case["doc_id"]), int(case["page"]))
        hit = any((str(r.get("DOC_ID", r.get("doc_id", ""))),
                   int(r.get("PAGE", r.get("page", 0)))) == expected for r in results)
        hits.append(hit)
    elapsed_ms = (time.perf_counter() - started) * 1000
    credits_after = service_daily_credits(session, service=service)
    return {
        "queries": len(cases),
        "recall_at_k": sum(hits) / len(hits) if hits else 0.0,
        "top_k": top_k,
        "elapsed_ms": round(elapsed_ms, 3),
        "cortex_search_daily_credits_before": credits_before,
        "cortex_search_daily_credits_after": credits_after,
        "daily_credit_delta": credits_after - credits_before,
        "cost_scope": "service daily aggregate, not per-query attribution",
    }
