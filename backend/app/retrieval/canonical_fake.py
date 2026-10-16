"""Document-level retrieval for the fake profile; frozen record-level studies stay intact."""
from __future__ import annotations

from typing import Sequence

from backend.contracts.fakes import FakeRetrieval, _load_corpus
from backend.contracts.models import Paper, ScoredPaper


class CanonicalFakeRetrieval(FakeRetrieval):
    """Keep the vector index's first-record identity and the fake's lexical scoring."""

    def search(
        self, query: str, *, secondary_query: str | None = None, top_k: int = 10,
        apply_rarity: bool = True, exclude_pmids: Sequence[str] = (),
    ) -> list[ScoredPaper]:
        if top_k <= 0:
            return []
        canonical = {paper.pmid: paper for paper in self.get_by_pmids(
            [paper.pmid for paper in _load_corpus()])}
        # The fixed corpus is bounded. Filter before truncating so duplicates
        # cannot consume slots or substitute another record's condition/text.
        ranked = super().search(
            query, secondary_query=secondary_query, top_k=len(_load_corpus()),
            apply_rarity=apply_rarity, exclude_pmids=exclude_pmids,
        )
        selected, seen = [], set()
        for item in ranked:
            pmid = item.paper.pmid
            if pmid in seen or item.paper != canonical[pmid]:
                continue
            seen.add(pmid)
            selected.append(item)
            if len(selected) == top_k:
                break
        return selected

    def get_by_pmids(self, pmids: Sequence[str]) -> list[Paper]:
        canonical = {}
        for paper in super().get_by_pmids(pmids):
            canonical.setdefault(paper.pmid, paper)
        return list(canonical.values())

    def health(self) -> dict:
        return {"ok": True, "detail": f"canonical fake retrieval, {len({p.pmid for p in _load_corpus()})} documents"}
