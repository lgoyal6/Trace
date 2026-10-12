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
from pathlib import Path
from typing import Sequence

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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS papers (
                pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
                journal TEXT NOT NULL, year INTEGER NOT NULL, condition TEXT NOT NULL,
                is_rare INTEGER NOT NULL, url TEXT NOT NULL, vector_json TEXT NOT NULL
            )""")
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
                    "INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(str(row["pmid"]), row["title"], row["abstract"],
                      row.get("journal", ""), int(row.get("year", 0)), row["condition"],
                      int(row.get("rarity") == "rare"),
                      row.get("url", f"https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}/"),
                      json.dumps(_embed(f"{row['title']} {row['abstract']}"))) for row in unique_rows],
                )

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
