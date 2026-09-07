from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.app.corpus.layout_parse import DocChunk, parse_layout_llamaparse
from backend.snowflake.layout_retrieval import (
    delete_document,
    index_chunks,
    search_chunks,
    service_daily_credits,
)


class Result:
    def __init__(self, rows=()): self.rows = rows
    def collect(self): return self.rows


class Session:
    def __init__(self): self.calls = []
    def sql(self, sql, params=None):
        self.calls.append((sql, params or []))
        if "SEARCH_PREVIEW" in sql:
            return Result([(json.dumps({"results": [{"DOC_ID": "doc", "PAGE": 2}]}),)])
        if "SELECT COUNT" in sql:
            return Result([(0,)])
        return Result()


def test_llamaparse_preserves_page_and_source_hash(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-contract")
    client = SimpleNamespace(
        load_data=lambda _: [
            SimpleNamespace(
                text="page text", metadata={"page_number": 2, "job_id": "job-1"}
            )
        ]
    )
    chunks = parse_layout_llamaparse(pdf, "doc", client=client)
    assert chunks[0].page == 2
    assert chunks[0].citation == "doc p.2"
    assert len(chunks[0].meta["source_sha256"]) == 64
    assert chunks[0].meta["llamaparse_job_id"] == "job-1"


def test_llamaparse_uses_live_job_result_shape(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-live-shape")

    class JobResult:
        job_id = "job-live-1"

        def get_markdown_documents(self, *, split_by_page):
            assert split_by_page is True
            return [
                SimpleNamespace(text="first page", metadata={"page_number": 1}),
                SimpleNamespace(text="second page", metadata={"page_number": 2}),
            ]

    class Client:
        def parse(self, path):
            assert path == str(pdf)
            return JobResult()

        def load_data(self, _):
            raise AssertionError("JobResult path must not call load_data")

    chunks = parse_layout_llamaparse(pdf, "doc", client=Client())
    assert [chunk.page for chunk in chunks] == [1, 2]
    assert {chunk.meta["llamaparse_job_id"] for chunk in chunks} == {"job-live-1"}


def test_index_query_delete_use_bound_document_values():
    session = Session()
    chunk = DocChunk("doc", 2, 0, "body", "text", "llamaparse",
                     meta={"source_sha256": "abc", "llamaparse_job_id": "job-1"})
    assert index_chunks(session, [chunk], warehouse="TRACE_C31_WH") == {
        "documents": 1, "chunks": 1
    }
    assert search_chunks(session, "query")[0]["PAGE"] == 2
    assert delete_document(session, "doc")["remaining_chunks"] == 0
    sql = " ".join(call[0] for call in session.calls)
    assert "CREATE CORTEX SEARCH SERVICE IF NOT EXISTS" in sql
    assert "WAREHOUSE = TRACE_C31_WH" in sql
    assert "AUTO_SUSPEND = 60" in sql
    assert "ALTER CORTEX SEARCH SERVICE" in sql
    assert "doc" not in sql
    assert any("doc" in params for _, params in session.calls)


def test_credit_query_scopes_the_full_service_name():
    session = Session()
    assert service_daily_credits(
        session, service="NEULIT.CORE.LAYOUT_SEARCH"
    ) == 0.0
    sql, params = session.calls[-1]
    assert "DATABASE_NAME = ?" in sql
    assert "SCHEMA_NAME = ?" in sql
    assert params == ["NEULIT", "CORE", "LAYOUT_SEARCH"]


def test_isolated_personal_database_identifiers_are_safe():
    session = Session()
    chunk = DocChunk("doc", 1, 0, "body", "text", "layout_pymupdf")
    assert index_chunks(
        session,
        [chunk],
        table="USER$LGOYAL6.LOCAL.LAYOUT_CHUNKS",
        service="USER$LGOYAL6.LOCAL.LAYOUT_SEARCH",
        warehouse="COMPUTE_WH",
    )["chunks"] == 1


def test_warehouse_identifier_rejects_sql_fragments():
    session = Session()
    chunk = DocChunk("doc", 1, 0, "body", "text", "layout_pymupdf")
    with pytest.raises(ValueError, match="unsafe Snowflake identifier"):
        index_chunks(session, [chunk], warehouse="WH; DROP DATABASE NEULIT")
    assert session.calls == []
