"""Persistent local vector retrieval implementing the frozen RetrievalPort.

SQLite persists public-corpus records and deterministic hashed embeddings. This
is an optional development/evaluation backend, not a replacement for managed
Snowflake Cortex Search or a claim about production ANN scale.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping, Sequence

from backend.app.corpus.versioning import content_hash
from backend.app.retrieval.rarity import rarity_boost
from backend.contracts.models import ConditionMatch, Paper, ScoredPaper

_WORD_RE = re.compile(r"[a-z0-9]+")
_DIMENSIONS = 256
_DEFAULT_CORPUS = Path(__file__).resolve().parent.parent / "data" / "corpus.json"
_DEFAULT_DB = Path("~/.codex/work/neulit/local-vector-store.sqlite3").expanduser()


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _embed(text: str) -> list[float]:
    """Stable word and adjacent-token feature-hash unit vector."""
    tokens = _tokens(text)
    features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    vector = [0.0] * _DIMENSIONS
    for feature in features:
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        number = int.from_bytes(digest, "big")
        vector[number % _DIMENSIONS] += 1.0 if (number >> 8) & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _paper(row: sqlite3.Row) -> Paper:
    return Paper(
        pmid=str(row["pmid"]), title=row["title"], abstract=row["abstract"],
        journal=row["journal"], year=int(row["year"]), condition=row["condition"],
        is_rare=bool(row["is_rare"]), url=row["url"],
    )


class LocalVectorRetriever:
    """Persistent local vector retrieval with the project's frozen port."""

    def __init__(self, db_path: str | Path | None = None,
                 corpus_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or os.environ.get("NEULIT_LOCAL_VECTOR_DB", _DEFAULT_DB)).expanduser()
        self.corpus_path = Path(corpus_path or _DEFAULT_CORPUS)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            # SQLite's context manager commits or rolls back but does not close.
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS papers (
                pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
                journal TEXT NOT NULL, year INTEGER NOT NULL, condition TEXT NOT NULL,
                is_rare INTEGER NOT NULL, url TEXT NOT NULL, vector_json TEXT NOT NULL,
                source_document TEXT, page INTEGER, metadata_json TEXT NOT NULL DEFAULT '{}'
            )""")
            # Keep databases created by the first local-vector release readable.
            columns = {row[1] for row in db.execute("PRAGMA table_info(papers)")}
            if "source_document" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN source_document TEXT")
            if "page" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN page INTEGER")
            if "metadata_json" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}' ")
            if "content_hash" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN content_hash TEXT")
            if "source_version" not in columns:
                db.execute("ALTER TABLE papers ADD COLUMN source_version INTEGER NOT NULL DEFAULT 1")
            if db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0:
                with self.corpus_path.open() as source:
                    rows = json.load(source)
                # The public fixture intentionally contains a near-duplicate
                # PMID; a vector index has one canonical record per document.
                unique_rows = []
                seen_pmids: set[str] = set()
                for row in rows:
                    pmid = str(row["pmid"])
                    if pmid in seen_pmids:
                        continue
                    seen_pmids.add(pmid)
                    unique_rows.append(row)
                db.executemany(
                    "INSERT INTO papers (pmid, title, abstract, journal, year, condition, is_rare, url, vector_json, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(str(row["pmid"]), row["title"], row["abstract"],
                      row.get("journal", ""), int(row.get("year", 0)), row["condition"],
                      int(row.get("rarity") == "rare"),
                      row.get("url", f"https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}/"),
                      json.dumps(_embed(f"{row['title']} {row['abstract']}")),
                      content_hash(pmid=str(row["pmid"]), title=row["title"], abstract=row["abstract"]))
                     for row in unique_rows],
                )
            # Rows written before content hashes existed get one now, so a
            # later sync can tell changed sources from unchanged ones.
            for row in db.execute("SELECT pmid, title, abstract FROM papers WHERE content_hash IS NULL").fetchall():
                db.execute("UPDATE papers SET content_hash = ? WHERE pmid = ?", (
                    content_hash(pmid=row["pmid"], title=row["title"], abstract=row["abstract"]),
                    row["pmid"]))

    def upsert_document(
        self,
        *,
        pmid: str,
        title: str,
        abstract: str,
        journal: str = "",
        year: int = 0,
        condition: str = "",
        is_rare: bool = False,
        url: str = "",
        source_document: str | None = None,
        page: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Insert or replace one document, retaining source and page metadata.

        The operation is idempotent by PMID. ``metadata`` is JSON so the local
        store stays dependency-free and its provenance can be inspected directly.
        """
        if page is not None and page < 1:
            raise ValueError("page must be a positive integer")
        with self._connect() as db:
            self._upsert(db, pmid=pmid, title=title, abstract=abstract, journal=journal,
                         year=year, condition=condition, is_rare=is_rare, url=url,
                         source_document=source_document, page=page, metadata=metadata)

    @staticmethod
    def _upsert(db: sqlite3.Connection, *, pmid: str, title: str, abstract: str,
                journal: str = "", year: int = 0, condition: str = "",
                is_rare: bool = False, url: str = "", source_document: str | None = None,
                page: int | None = None, metadata: Mapping[str, object] | None = None) -> str:
        """Write one record inside the caller's transaction.

        Returns "inserted", "updated" or "unchanged". ``source_version`` goes up
        by one only when the content hash changes, so re-running an identical
        sync is a no-op and a version number always names one text.
        """
        new_hash = content_hash(pmid=str(pmid), title=title, abstract=abstract)
        record_metadata = json.dumps(dict(metadata or {}), sort_keys=True)
        existing = db.execute(
            "SELECT content_hash, source_version, source_document, page, metadata_json, journal, year, url, "
            "condition, is_rare "
            "FROM papers WHERE pmid = ?", (str(pmid),),
        ).fetchone()
        if existing is not None and existing["content_hash"] == new_hash and (
            existing["source_document"], existing["page"], existing["metadata_json"],
            existing["journal"], int(existing["year"]), existing["url"],
            existing["condition"], bool(existing["is_rare"]),
        ) == (source_document, page, record_metadata, journal, int(year), url, condition, bool(is_rare)):
            return "unchanged"
        version = 1
        if existing is not None:
            version = int(existing["source_version"] or 1)
            if existing["content_hash"] != new_hash:
                version += 1
        db.execute(
            """INSERT INTO papers
            (pmid, title, abstract, journal, year, condition, is_rare, url,
             vector_json, source_document, page, metadata_json, content_hash, source_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pmid) DO UPDATE SET
              title=excluded.title, abstract=excluded.abstract,
              journal=excluded.journal, year=excluded.year,
              condition=excluded.condition, is_rare=excluded.is_rare,
              url=excluded.url, vector_json=excluded.vector_json,
              source_document=excluded.source_document, page=excluded.page,
              metadata_json=excluded.metadata_json, content_hash=excluded.content_hash,
              source_version=excluded.source_version""",
            (str(pmid), title, abstract, journal, int(year), condition,
             int(is_rare), url, json.dumps(_embed(f"{title} {abstract}")), source_document, page,
             record_metadata, new_hash, version),
        )
        return "inserted" if existing is None else "updated"

    def sync_records(self, records: Sequence[Mapping[str, object]], *,
                     tombstones=None, prune: bool = True) -> dict[str, object]:
        """Make the store match ``records`` (corpus.json rows) in one transaction.

        Changed text is re-embedded and its version bumped, unchanged rows are
        left alone, rows absent from ``records`` are removed when ``prune`` is
        true, and a ``TombstoneLog`` wins over the records: a deleted pmid is
        removed even if the source still lists it, a redacted one is stored
        with the placeholder text. All-or-nothing: an exception rolls back, so
        a failed sync leaves the previous index intact rather than half new.
        """
        from backend.app.corpus.retention import apply_to_records

        rows = [dict(r) for r in records]
        deleted = set(tombstones.deleted_pmids()) if tombstones is not None else set()
        if tombstones is not None:
            rows = apply_to_records(rows, tombstones)
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        wanted: set[str] = set()
        with self._connect() as db:
            for row in rows:
                pmid = str(row["pmid"])
                if pmid in wanted:
                    continue  # one canonical record per document, first wins
                wanted.add(pmid)
                outcome = self._upsert(
                    db, pmid=pmid, title=str(row["title"]), abstract=str(row["abstract"]),
                    journal=str(row.get("journal", "")), year=int(row.get("year", 0) or 0),
                    condition=str(row.get("condition", "")), is_rare=row.get("rarity") == "rare",
                    url=str(row.get("url", f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/")),
                )
                counts[outcome] += 1
            # Prune only corpus rows; page chunks indexed through
            # replace_document_chunks belong to their own source document.
            corpus_rows = {r[0] for r in db.execute("SELECT pmid FROM papers WHERE source_document IS NULL")}
            stored = {r[0] for r in db.execute("SELECT pmid FROM papers")}
            to_remove = (corpus_rows - wanted) if prune else set()
            to_remove |= stored & deleted
            for pmid in sorted(to_remove):
                db.execute("DELETE FROM papers WHERE pmid = ?", (pmid,))
            counts["deleted"] = len(to_remove)
        return {"store": str(self.db_path), **counts, "records": len(wanted)}

    def replace_document_chunks(self, doc_id: str, parse, *, condition: str = "",
                                is_rare: bool = False) -> dict[str, object]:
        """Re-index every page chunk of one source document, all or nothing.

        ``parse`` is a zero-argument callable returning layout_parse.DocChunk
        objects. If it raises or yields no usable chunk, nothing is written and
        the previously indexed version of the document keeps serving; the
        receipt says why. On success the old chunks of ``doc_id`` are replaced
        in the same transaction as the new ones are written.
        """
        try:
            chunks = [c for c in parse() if str(getattr(c, "text", "")).strip()]
        except Exception as exc:  # parser failure is data, not a crash
            return {"doc_id": doc_id, "status": "parser_failed", "error": type(exc).__name__,
                    "written": 0}
        if not chunks:
            return {"doc_id": doc_id, "status": "no_chunks", "written": 0}
        with self._connect() as db:
            db.execute("DELETE FROM papers WHERE source_document = ?", (doc_id,))
            for chunk in chunks:
                chunk_id = f"{doc_id}#p{chunk.page}b{chunk.block_index}"
                self._upsert(
                    db, pmid=chunk_id, title=f"{doc_id} p.{chunk.page}", abstract=chunk.text,
                    condition=condition, is_rare=is_rare, source_document=doc_id,
                    page=chunk.page if chunk.page > 0 else None,
                    metadata={"parser": chunk.parser, "kind": chunk.kind, "block": chunk.block_index},
                )
        return {"doc_id": doc_id, "status": "indexed", "written": len(chunks)}

    def version(self, pmid: str) -> dict[str, object] | None:
        """content_hash and source_version for one stored record."""
        with self._connect() as db:
            row = db.execute("SELECT content_hash, source_version FROM papers WHERE pmid = ?",
                             (str(pmid),)).fetchone()
        if row is None:
            return None
        return {"content_hash": row["content_hash"], "source_version": int(row["source_version"])}

    def delete_document(self, pmid: str) -> bool:
        """Delete a document and return whether a record was removed."""
        with self._connect() as db:
            result = db.execute("DELETE FROM papers WHERE pmid = ?", (str(pmid),))
            return result.rowcount == 1

    def provenance(self, pmid: str) -> dict[str, object] | None:
        """Return source-document, page, and caller metadata for one record."""
        with self._connect() as db:
            row = db.execute(
                "SELECT source_document, page, metadata_json FROM papers WHERE pmid = ?",
                (str(pmid),),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_document": row["source_document"],
            "page": row["page"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def search(self, query: str, *, secondary_query: str | None = None,
               top_k: int = 10, apply_rarity: bool = True,
               exclude_pmids: Sequence[str] = ()) -> list[ScoredPaper]:
        query_vector = _embed(f"{query} {secondary_query or ''}")
        excluded = set(exclude_pmids)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM papers").fetchall()
        scored: list[ScoredPaper] = []
        for row in rows:
            paper = _paper(row)
            if paper.pmid in excluded:
                continue
            semantic = max(0.0, _cosine(query_vector, json.loads(row["vector_json"])))
            multiplier = rarity_boost({"rarity": "rare" if paper.is_rare else "common"}) if apply_rarity else 1.0
            scored.append(ScoredPaper(
                paper=paper, score=semantic * multiplier, lexical_score=0.0,
                semantic_score=semantic, rarity_multiplier=multiplier,
            ))
        scored.sort(key=lambda item: (item.score, item.paper.pmid), reverse=True)
        return scored[:max(0, top_k)]

    def closest_conditions(self, query: str, top_n: int = 3) -> list[ConditionMatch]:
        query_vector = _embed(query)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM papers").fetchall()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[row["condition"]].append(row)
        matches = []
        for condition, condition_rows in grouped.items():
            similarity = max((_cosine(query_vector, json.loads(row["vector_json"])) for row in condition_rows), default=0.0)
            matches.append(ConditionMatch(
                condition=condition, similarity=similarity,
                paper_count=len(condition_rows), is_rare=bool(condition_rows[0]["is_rare"]),
            ))
        matches.sort(key=lambda item: (item.similarity, item.condition), reverse=True)
        return matches[:max(0, top_n)]

    def get_by_pmids(self, pmids: Sequence[str]) -> list[Paper]:
        if not pmids:
            return []
        placeholders = ",".join("?" for _ in pmids)
        with self._connect() as db:
            rows = db.execute(f"SELECT * FROM papers WHERE pmid IN ({placeholders})", list(pmids)).fetchall()
        by_pmid = {row["pmid"]: _paper(row) for row in rows}
        return [by_pmid[pmid] for pmid in pmids if pmid in by_pmid]

    def health(self) -> dict:
        try:
            with self._connect() as db:
                count = int(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
            return {"ok": count > 0, "detail": f"local vector store, {count} papers", "path": str(self.db_path)}
        except sqlite3.Error as exc:
            return {"ok": False, "detail": f"local vector store unavailable: {exc}"}
