"""Corpus build/migration.

Two responsibilities, kept in one module because they share the same
Condition list and JSON shape:

1. `build_corpus()` - the original one-time PubMed fetch (Step 2.5), unchanged
   in behavior. `python -m backend.app.corpus.build_corpus` still fetches from
   PubMed and writes backend/data/corpus.json.

2. `load_to_snowflake()` - the v2 migration target. `python -m
   backend.app.corpus.build_corpus --to-snowflake` reads the existing
   backend/data/corpus.json (329 papers, 14 conditions; the JSON stays
   in-repo as migration source + fake fixture, per
   plan-v2/00-SHARED-CONTRACTS.md) and idempotently (truncate+reload) writes
   NEULIT.CORE.PAPERS and NEULIT.CORE.CONDITIONS.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from backend.app.corpus.conditions import CONDITIONS, Condition
from backend.app.corpus.fetch_pubmed import fetch_abstracts, search_pmids

DEFAULT_OUT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "corpus.json"


def build_corpus(conditions: list[Condition], out_path: Path) -> list[dict]:
    """Original PubMed fetch. Output sink unchanged: writes JSON to out_path."""
    papers: list[dict] = []
    for condition in conditions:
        pmids = search_pmids(condition.pubmed_query, retmax=condition.target_count)
        time.sleep(0.4)  # stay under the ~3 req/sec unauthenticated E-utils limit
        fetched = fetch_abstracts(pmids)
        time.sleep(0.4)
        for paper in fetched:
            papers.append({
                **paper,
                "condition": condition.name,
                "rarity": condition.rarity,
                "region_literature": condition.region_literature,
                "atlas_label": condition.atlas_label,
                "overlaps_with": condition.overlaps_with,
            })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(papers, indent=2))
    return papers


def _condition_description(name: str, papers: list[dict]) -> str:
    """Deterministic short description used as the EMBED_TEXT_768 input for
    CONDITIONS.CONDITION_VEC. Built from the region literature + atlas label
    already present on every paper row tagged with this condition, so it
    needs no new hand-authored copy.
    """
    if not papers:
        return name
    sample = papers[0]
    region = sample.get("region_literature", "")
    atlas = sample.get("atlas_label", "")
    return f"{name}. Region literature: {region}. Atlas regions: {atlas}."


def load_to_snowflake(corpus_path: Path = DEFAULT_OUT_PATH) -> dict:
    """Idempotent load of backend/data/corpus.json into NEULIT.CORE.PAPERS
    and NEULIT.CORE.CONDITIONS. Truncates and reloads both tables so a
    re-run never leaves a half-loaded state.

    Returns a summary dict {"papers": n, "conditions": n, "rare_conditions": n}.
    Degrades to a RuntimeError with a clear message if Snowflake is
    unavailable - this is an operator-run migration script, not a
    request-path call, so raising (rather than silently no-op'ing) is
    correct here.
    """
    from backend.snowflake.session import get_session, snowflake_available

    if not snowflake_available():
        raise RuntimeError(
            "load_to_snowflake: SNOWFLAKE_* credentials not set / snowpark unavailable. "
            "This is a by-hand migration step; see snowflake/sql/README.md."
        )
    session = get_session()
    assert session is not None

    papers = json.loads(corpus_path.read_text())

    session.sql("TRUNCATE TABLE NEULIT.CORE.PAPERS").collect()
    session.sql("TRUNCATE TABLE NEULIT.CORE.CONDITIONS").collect()

    paper_rows = []
    by_condition: dict[str, list[dict]] = {}
    for p in papers:
        by_condition.setdefault(p["condition"], []).append(p)
        title = (p.get("title") or "").replace("'", "''")
        abstract = (p.get("abstract") or "").replace("'", "''")
        condition = p["condition"].replace("'", "''")
        journal = (p.get("journal") or "").replace("'", "''")
        url = p.get("url") or f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"
        url = url.replace("'", "''")
        is_rare = "TRUE" if p.get("rarity") == "rare" else "FALSE"
        pub_year = p.get("year")
        pub_year_sql = str(int(pub_year)) if pub_year else "NULL"
        search_blob = f"{title} {abstract}".replace("'", "''")
        paper_rows.append(
            f"('{p['pmid']}', '{title}', '{abstract}', '{journal}', {pub_year_sql}, "
            f"'{condition}', {is_rare}, '{url}', '{search_blob}')"
        )

    # Multi-row INSERT, not row-by-row.
    if paper_rows:
        for i in range(0, len(paper_rows), 200):
            chunk = ",\n".join(paper_rows[i:i + 200])
            session.sql(
                "INSERT INTO NEULIT.CORE.PAPERS "
                "(PMID, TITLE, ABSTRACT, JOURNAL, PUB_YEAR, CONDITION, IS_RARE, URL, SEARCH_BLOB) "
                f"VALUES {chunk}"
            ).collect()

    # SELECT ... UNION ALL ..., not VALUES (...) - Snowflake's VALUES clause
    # rejects function calls like ARRAY_CONSTRUCT inside literal row tuples.
    condition_rows = []
    for name, rows in by_condition.items():
        is_rare = "TRUE" if rows[0].get("rarity") == "rare" else "FALSE"
        description = _condition_description(name, rows).replace("'", "''")
        regions = sorted({rows[0].get("atlas_label", "")})
        regions_sql = "ARRAY_CONSTRUCT(" + ",".join(f"'{r.replace(chr(39), chr(39)*2)}'" for r in regions if r) + ")"
        name_sql = name.replace("'", "''")
        condition_rows.append(
            f"SELECT '{name_sql}' AS CONDITION, {is_rare} AS IS_RARE, {len(rows)} AS PAPER_COUNT, "
            f"'{description}' AS DESCRIPTION, {regions_sql} AS BRAIN_REGIONS"
        )

    if condition_rows:
        union_sql = "\nUNION ALL\n".join(condition_rows)
        session.sql(
            "INSERT INTO NEULIT.CORE.CONDITIONS "
            "(CONDITION, IS_RARE, PAPER_COUNT, DESCRIPTION, BRAIN_REGIONS) "
            f"{union_sql}"
        ).collect()

    # CONDITION_VEC filled with a single UPDATE using Cortex EMBED_TEXT_768,
    # not row by row.
    session.sql(
        "UPDATE NEULIT.CORE.CONDITIONS "
        "SET CONDITION_VEC = SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m', DESCRIPTION)"
    ).collect()

    rare_count = sum(1 for rows in by_condition.values() if rows[0].get("rarity") == "rare")
    return {
        "papers": len(paper_rows),
        "conditions": len(condition_rows),
        "rare_conditions": rare_count,
    }


if __name__ == "__main__":
    if "--to-snowflake" in sys.argv:
        summary = load_to_snowflake(DEFAULT_OUT_PATH)
        print(
            f"Loaded {summary['papers']} papers across {summary['conditions']} "
            f"conditions ({summary['rare_conditions']} rare) -> NEULIT.CORE.PAPERS/CONDITIONS"
        )
    else:
        result = build_corpus(CONDITIONS, DEFAULT_OUT_PATH)
        print(f"Fetched {len(result)} papers across {len(CONDITIONS)} conditions -> {DEFAULT_OUT_PATH}")
