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
from backend.app.corpus.fetch_pubmed import fetch_abstracts_with_provenance, search_pmids
from backend.app.corpus.provenance import DEFAULT_MANIFEST_PATH, Manifest
from backend.app.corpus.retention import TombstoneLog, apply_to_records

DEFAULT_OUT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "corpus.json"


def build_corpus(
    conditions: list[Condition],
    out_path: Path,
    *,
    manifest_path: Path | None = None,
    sleep_seconds: float = 0.4,
) -> list[dict]:
    """Original PubMed fetch. Output sink unchanged: writes JSON to out_path.

    Now also writes a provenance manifest beside it, binding every stored
    record to the sha256 of the raw efetch bytes it was derived from, the
    fetch time, the request URL, and the parser version. An unchanged document
    parsed by the same parser is not re-derived: `Manifest.needs_reprocess`
    says so, and the records already recorded for that document are reused.
    That skips the parse and the derivation, not the fetch - the hash is of
    the response, so the bytes are already here by the time it can answer.
    """
    # Derived from out_path rather than hardcoded, so the real run lands on
    # DEFAULT_MANIFEST_PATH (same directory as DEFAULT_OUT_PATH) and a test
    # building into tmp_path keeps its manifest in tmp_path too.
    manifest_path = Path(manifest_path or out_path.parent / DEFAULT_MANIFEST_PATH.name)
    manifest = Manifest.load(manifest_path)
    prior_by_pmid = {
        str(r.get("pmid", "")): r
        for r in (json.loads(out_path.read_text()) if out_path.exists() else [])
    }

    papers: list[dict] = []
    reused = 0
    for condition in conditions:
        pmids = search_pmids(condition.pubmed_query, retmax=condition.target_count)
        time.sleep(sleep_seconds)  # stay under the ~3 req/sec unauthenticated E-utils limit
        fetched, source = fetch_abstracts_with_provenance(pmids)
        time.sleep(sleep_seconds)
        if source is None:
            continue

        should_reprocess, _reason = manifest.needs_reprocess(source)
        if not should_reprocess:
            # Same bytes, same parser: whatever we derived last time is still
            # what we would derive now. Reuse the stored records rather than
            # rebuilding identical ones.
            carried = [
                prior_by_pmid[p]
                for p in (
                    prov.pmid
                    for prov in manifest.records.values()
                    if prov.source_url == source.url
                )
                if p in prior_by_pmid
            ]
            if carried:
                papers.extend(carried)
                reused += len(carried)
                continue

        manifest.record_fetch(source)
        for paper in fetched:
            record = {
                **paper,
                "condition": condition.name,
                "rarity": condition.rarity,
                "region_literature": condition.region_literature,
                "atlas_label": condition.atlas_label,
                "overlaps_with": condition.overlaps_with,
            }
            manifest.record_derived(record, source)
            papers.append(record)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(papers, indent=2))
    manifest.save(manifest_path)
    if reused:
        print(f"reused {reused} record(s) from unchanged documents")
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

    # The tombstone log, not the JSON file, is the authority on what may be
    # loaded. This truncates and reloads, so without this filter a reload from
    # any corpus.json that predates a deletion (a git checkout, a restored
    # backup, a re-fetch) silently resurrects the deleted record into PAPERS
    # and from there into the Cortex Search index. The log survives all three.
    log = TombstoneLog.load()
    if log:
        before = len(papers)
        papers = apply_to_records(papers, log)
        print(
            f"retention: {before - len(papers)} deleted record(s) withheld from the load, "
            f"{sum(1 for p in papers if p.get('redacted'))} redacted"
        )

    session.sql("TRUNCATE TABLE NEULIT.CORE.PAPERS").collect()
    session.sql("TRUNCATE TABLE NEULIT.CORE.CONDITIONS").collect()

    # Every string here is document text that came off the network via
    # backend/app/corpus/fetch_pubmed.py, so it is bound, not escaped. Doubling
    # `\'` is not enough on its own: Snowflake also honours backslash escapes
    # inside a string literal, so a `\\'` in an abstract closes a literal that
    # quote-doubling believes it neutralised, and the rest of the abstract is
    # parsed as SQL. IS_RARE stays a literal because it is a Python bool this
    # function computes, never text from a paper.
    paper_rows: list[str] = []
    paper_params: list = []
    by_condition: dict[str, list[dict]] = {}
    for p in papers:
        by_condition.setdefault(p["condition"], []).append(p)
        title = p.get("title") or ""
        abstract = p.get("abstract") or ""
        url = p.get("url") or f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"
        pub_year = p.get("year")
        is_rare = "TRUE" if p.get("rarity") == "rare" else "FALSE"
        paper_rows.append(f"(?, ?, ?, ?, ?, ?, {is_rare}, ?, ?)")
        paper_params.extend([
            p["pmid"], title, abstract, p.get("journal") or "",
            int(pub_year) if pub_year else None, p["condition"], url,
            f"{title} {abstract}",
        ])

    # Multi-row INSERT, not row-by-row.
    if paper_rows:
        per_row = len(paper_params) // len(paper_rows)
        for i in range(0, len(paper_rows), 200):
            chunk = ",\n".join(paper_rows[i:i + 200])
            session.sql(
                "INSERT INTO NEULIT.CORE.PAPERS "
                "(PMID, TITLE, ABSTRACT, JOURNAL, PUB_YEAR, CONDITION, IS_RARE, URL, SEARCH_BLOB) "
                f"VALUES {chunk}",
                params=paper_params[i * per_row:(i + 200) * per_row],
            ).collect()

    # SELECT ... UNION ALL ..., not VALUES (...) - Snowflake's VALUES clause
    # rejects function calls like ARRAY_CONSTRUCT inside literal row tuples.
    condition_rows: list[str] = []
    condition_params: list = []
    for name, rows in by_condition.items():
        is_rare = "TRUE" if rows[0].get("rarity") == "rare" else "FALSE"
        regions = [r for r in sorted({rows[0].get("atlas_label", "")}) if r]
        regions_sql = "ARRAY_CONSTRUCT(" + ",".join("?" for _ in regions) + ")"
        condition_rows.append(
            f"SELECT ? AS CONDITION, {is_rare} AS IS_RARE, {len(rows)} AS PAPER_COUNT, "
            f"? AS DESCRIPTION, {regions_sql} AS BRAIN_REGIONS"
        )
        condition_params.extend([name, _condition_description(name, rows), *regions])

    if condition_rows:
        union_sql = "\nUNION ALL\n".join(condition_rows)
        session.sql(
            "INSERT INTO NEULIT.CORE.CONDITIONS "
            "(CONDITION, IS_RARE, PAPER_COUNT, DESCRIPTION, BRAIN_REGIONS) "
            f"{union_sql}",
            params=condition_params,
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
