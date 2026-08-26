"""CortexSearchRetriever - implements backend.contracts.ports.RetrievalPort
against Snowflake Cortex Search Service (native hybrid lexical + vector),
with the rarity boost applied as a post-retrieval re-rank.

Every method degrades to an empty/default result and logs a warning if
Snowflake is unavailable. Never raises.
"""
from __future__ import annotations

import logging
from typing import Sequence

from backend.app.retrieval.rarity import rarity_boost
from backend.contracts.models import ConditionMatch, Paper, ScoredPaper
from backend.snowflake.session import get_session, snowflake_available

logger = logging.getLogger("neulit.snowflake.retrieval")

import os

_SEARCH_SERVICE = os.environ.get("SNOWFLAKE_SEARCH_SERVICE", "NEULIT.CORE.PAPERS_SEARCH")
_SECONDARY_WEIGHT = 0.4
_PRIMARY_WEIGHT = 0.6


def _row_to_paper(row: dict) -> Paper:
    return Paper(
        pmid=str(row.get("PMID", row.get("pmid", ""))),
        title=row.get("TITLE", row.get("title", "")) or "",
        abstract=row.get("ABSTRACT", row.get("abstract", "")) or "",
        journal=row.get("JOURNAL", row.get("journal", "")) or "",
        year=int(row.get("PUB_YEAR", row.get("pub_year", 0)) or 0),
        condition=row.get("CONDITION", row.get("condition", "")) or "",
        is_rare=bool(row.get("IS_RARE", row.get("is_rare", False))),
        url=row.get("URL", row.get("url", "")) or "",
    )


def _normalize(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a {pmid: raw_score} map to [0, 1]. Same
    normalization v1's _normalize performed. Constant-score sets map to 1.0
    for every member (no division by zero).
    """
    if not scores:
        return {}
    lo = min(scores.values())
    hi = max(scores.values())
    if hi == lo:
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


class CortexSearchRetriever:
    def __init__(self, search_service: str | None = None) -> None:
        self._search_service = search_service or _SEARCH_SERVICE

    # -- internal: one raw Cortex Search call -----------------------------
    _RESULT_COLUMNS = ["PMID", "TITLE", "ABSTRACT", "JOURNAL", "PUB_YEAR", "CONDITION", "IS_RARE", "URL"]

    def _raw_search(self, query: str, limit: int) -> list[dict]:
        """Returns raw result rows (dicts) from the Cortex Search Service via
        SNOWFLAKE.CORTEX.SEARCH_PREVIEW. Each row carries the requested
        columns plus an '@scores' object with 'text_match', 'cosine_similarity',
        and 'reranker_score' sub-scores. Returns [] on any failure.
        """
        session = get_session()
        if session is None:
            return []
        try:
            import json as _json
            request = _json.dumps({
                "query": query,
                "limit": limit,
                "columns": self._RESULT_COLUMNS,
            })
            rows = session.sql(
                "SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(?, ?) AS RESP",
                params=[self._search_service, request],
            ).collect()
            if not rows:
                return []
            resp = rows[0][0]
            payload = _json.loads(resp) if isinstance(resp, str) else resp
            return payload.get("results", []) if isinstance(payload, dict) else []
        except Exception:
            logger.exception("CortexSearchRetriever._raw_search failed for service=%s", self._search_service)
            return []

    @staticmethod
    def _scores(row: dict) -> dict:
        s = row.get("@scores", {})
        return s if isinstance(s, dict) else {}

    # -- RetrievalPort ------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        secondary_query: str | None = None,
        top_k: int = 10,
        apply_rarity: bool = True,
        exclude_pmids: Sequence[str] = (),
    ) -> list[ScoredPaper]:
        if not snowflake_available():
            logger.warning("CortexSearchRetriever.search: Snowflake unavailable, returning []")
            return []

        over_fetch = max(top_k * 4, 40)
        primary_rows = self._raw_search(query, over_fetch)
        if not primary_rows:
            return []

        by_pmid_row: dict[str, dict] = {}
        primary_raw: dict[str, float] = {}
        lexical: dict[str, float] = {}
        semantic: dict[str, float] = {}
        for row in primary_rows:
            pmid = str(row.get("PMID", row.get("pmid", "")))
            if not pmid:
                continue
            by_pmid_row[pmid] = row
            scores = self._scores(row)
            primary_raw[pmid] = float(
                scores.get("reranker_score", scores.get("cosine_similarity", 0.0)) or 0.0
            )
            lexical[pmid] = float(scores.get("text_match", 0.0) or 0.0)
            semantic[pmid] = float(scores.get("cosine_similarity", 0.0) or 0.0)

        primary_norm = _normalize(primary_raw)

        if secondary_query:
            secondary_rows = self._raw_search(secondary_query, over_fetch)
            secondary_raw: dict[str, float] = {}
            for row in secondary_rows:
                pmid = str(row.get("PMID", row.get("pmid", "")))
                if not pmid:
                    continue
                by_pmid_row.setdefault(pmid, row)
                scores = self._scores(row)
                secondary_raw[pmid] = float(
                    scores.get("reranker_score", scores.get("cosine_similarity", 0.0)) or 0.0
                )
            secondary_norm = _normalize(secondary_raw)

            fused: dict[str, float] = {}
            for pmid in set(primary_norm) | set(secondary_norm):
                fused[pmid] = (
                    _PRIMARY_WEIGHT * primary_norm.get(pmid, 0.0)
                    + _SECONDARY_WEIGHT * secondary_norm.get(pmid, 0.0)
                )
            final_norm = fused
        else:
            final_norm = primary_norm

        excluded = set(exclude_pmids)
        scored: list[ScoredPaper] = []
        for pmid, base_score in final_norm.items():
            if pmid in excluded:
                continue
            row = by_pmid_row.get(pmid)
            if row is None:
                continue
            paper = _row_to_paper(row)
            multiplier = rarity_boost({"rarity": "rare" if paper.is_rare else "common"}) if apply_rarity else 1.0
            final_score = base_score * multiplier
            scored.append(
                ScoredPaper(
                    paper=paper,
                    score=final_score,
                    lexical_score=lexical.get(pmid, 0.0),
                    semantic_score=semantic.get(pmid, 0.0),
                    rarity_multiplier=multiplier,
                )
            )

        scored.sort(key=lambda sp: (sp.score, sp.paper.pmid), reverse=True)
        return scored[:top_k]

    def closest_conditions(self, query: str, top_n: int = 3) -> list[ConditionMatch]:
        if not snowflake_available():
            logger.warning("CortexSearchRetriever.closest_conditions: Snowflake unavailable, returning []")
            return []
        session = get_session()
        if session is None:
            return []
        try:
            rows = session.sql(
                "SELECT CONDITION, IS_RARE, PAPER_COUNT, "
                "VECTOR_COSINE_SIMILARITY(CONDITION_VEC, "
                "SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m', ?)) AS SIMILARITY "
                "FROM NEULIT.CORE.CONDITIONS "
                "ORDER BY SIMILARITY DESC LIMIT ?",
                params=[query, top_n],
            ).collect()
        except Exception:
            logger.exception("CortexSearchRetriever.closest_conditions failed")
            return []

        matches = []
        for row in rows:
            matches.append(
                ConditionMatch(
                    condition=row["CONDITION"],
                    similarity=float(row["SIMILARITY"] or 0.0),
                    paper_count=int(row["PAPER_COUNT"] or 0),
                    is_rare=bool(row["IS_RARE"]),
                )
            )
        return matches

    def get_by_pmids(self, pmids: Sequence[str]) -> list[Paper]:
        if not pmids or not snowflake_available():
            return []
        session = get_session()
        if session is None:
            return []
        try:
            placeholders = ",".join("?" for _ in pmids)
            rows = session.sql(
                "SELECT PMID, TITLE, ABSTRACT, JOURNAL, PUB_YEAR, CONDITION, IS_RARE, URL "
                f"FROM NEULIT.CORE.PAPERS WHERE PMID IN ({placeholders})",
                params=list(pmids),
            ).collect()
        except Exception:
            logger.exception("CortexSearchRetriever.get_by_pmids failed")
            return []

        by_pmid = {str(r["PMID"]): _row_to_paper(r.as_dict()) for r in rows}
        # order preserved to match the input sequence
        return [by_pmid[p] for p in pmids if p in by_pmid]

    def health(self) -> dict:
        if not snowflake_available():
            return {"ok": False, "detail": "Snowflake session unavailable"}
        session = get_session()
        if session is None:
            return {"ok": False, "detail": "Snowflake session unavailable"}
        try:
            service_rows = session.sql(
                f"SHOW CORTEX SEARCH SERVICES LIKE '{self._search_service.split('.')[-1]}' "
                "IN SCHEMA NEULIT.CORE"
            ).collect()
            active = any(
                str(r.as_dict().get("serving_state", r.as_dict().get("SERVING_STATE", ""))).upper() == "ACTIVE"
                for r in service_rows
            ) if service_rows else False
            count_row = session.sql("SELECT COUNT(*) AS N FROM NEULIT.CORE.PAPERS").collect()
            paper_count = int(count_row[0]["N"]) if count_row else 0
            ok = active and paper_count > 0
            return {
                "ok": ok,
                "detail": f"search_service_active={active} paper_count={paper_count}",
            }
        except Exception as exc:
            logger.exception("CortexSearchRetriever.health failed")
            return {"ok": False, "detail": f"health check failed: {exc}"}
