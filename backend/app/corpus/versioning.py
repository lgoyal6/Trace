"""Hash source text and revalidate stored citations against current records.

Only citations classified as current may be served unchanged.
See docs/corpus-controls.md for hashes and citation states.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from backend.app.corpus.provenance import record_hash
from backend.app.corpus.retention import REDACTION_PLACEHOLDER


def content_hash(*, pmid: str, title: str, abstract: str) -> str:
    """The manifest's `record_sha256` for a record with this text.

    Reuses provenance.record_hash over its DERIVED_FIELDS (pmid, title,
    abstract), so a vector-store version, a citation snapshot and the corpus
    manifest all name the same document version with the same string.
    Curation (condition, rarity) is deliberately outside it, as it is there.
    """
    return record_hash({"pmid": str(pmid), "title": title, "abstract": abstract})


def paper_hash(paper) -> str:
    """content_hash of a contracts.models.Paper."""
    return content_hash(pmid=paper.pmid, title=paper.title, abstract=paper.abstract)


@dataclass(frozen=True)
class CitationStatus:
    pmid: str
    status: str  # current | changed | revoked | redacted
    built_hash: str
    current_hash: str | None


@dataclass(frozen=True)
class RevalidationReport:
    citations: tuple[CitationStatus, ...]

    @property
    def servable(self) -> bool:
        return bool(self.citations) and all(c.status == "current" for c in self.citations)

    def counts(self) -> dict[str, int]:
        out = {"current": 0, "changed": 0, "revoked": 0, "redacted": 0}
        for c in self.citations:
            out[c.status] += 1
        return out


def snapshot(papers: Sequence) -> dict[str, str]:
    """pmid -> content hash for the record set an answer was built from."""
    return {sp.paper.pmid: paper_hash(sp.paper) for sp in papers}


def revalidate_citations(built: Mapping[str, str], port) -> RevalidationReport:
    """Classify each pmid in `built` (pmid -> hash at build time) against `port`."""
    pmids = list(built)
    current = {p.pmid: p for p in port.get_by_pmids(pmids)}
    statuses = []
    for pmid in pmids:
        paper = current.get(pmid)
        if paper is None:
            statuses.append(CitationStatus(pmid, "revoked", built[pmid], None))
            continue
        now = paper_hash(paper)
        if paper.abstract == REDACTION_PLACEHOLDER:
            status = "redacted"
        elif now == built[pmid]:
            status = "current"
        else:
            status = "changed"
        statuses.append(CitationStatus(pmid, status, built[pmid], now))
    return RevalidationReport(tuple(statuses))
