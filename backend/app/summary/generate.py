"""Sourced summary generation: every claim cites its source paper via a [N]
marker matching the numbered abstracts stuffed into the prompt, so a reader
can verify each sentence against its citation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from backend.app.llm.json_repair import try_parse_json
from backend.app.verify.grounding import RecordProvenance
from backend.contracts.models import Message, ScoredPaper
from backend.contracts.ports import LLMPort

SUMMARY_JSON_SCHEMA = {
    "type": "object",
    "properties": {"summary_markdown": {"type": "string"}},
    "required": ["summary_markdown"],
}

SUMMARY_SYSTEM_PROMPT = (
    "You are summarizing rare-case PET/neuroimaging literature for research purposes, "
    "not providing medical advice or diagnosis. Every claim must cite its source using "
    "[N] matching the numbered abstracts below. Do not state or imply a diagnosis for any "
    "individual patient. If a specific detail is not present in the retrieved abstracts, "
    "state 'not reported in retrieved abstracts' rather than omitting it or filling it in "
    "from general knowledge.\n\n"
    'Return ONLY valid JSON: {"summary_markdown": "<markdown summary, every sentence '
    'carrying a [N] citation>"}'
)


@dataclass
class SourcedCitation:
    """`index` is the [N] slot in the prompt. `retrieval_rank` and `source`
    are the provenance: which position in the retrieved ordering the record
    held, and which backend served it.

    Kept as separate fields rather than inferred from `index` because `index`
    is assigned while numbering abstracts for one prompt. Anything that
    reorders, filters or truncates between retrieval and that numbering
    changes it, and a rank you can only recover by trusting a prompt string is
    not something a stored answer can be audited against later.
    """

    index: int
    pmid: str
    supported: bool | None = None
    note: str | None = None
    source: str = ""
    retrieval_rank: int = 0


@dataclass
class SourcedSummary:
    markdown: str
    citations: list[SourcedCitation] = field(default_factory=list)
    degraded: bool = False


def _build_messages(query: str, papers: list[ScoredPaper], distilled_context: str) -> list[Message]:
    stuffed = "\n\n".join(
        f"[{i + 1}] PMID {sp.paper.pmid} - {sp.paper.title}\n{sp.paper.abstract}"
        for i, sp in enumerate(papers)
    )
    messages: list[Message] = []
    if distilled_context:
        messages.append(Message(role="system", content=(
            f"The reader is described as: {distilled_context}. Assume familiarity with "
            "material they have already explored; prioritize what is new to them. Do not "
            "mention this description in your answer."
        )))
    messages.append(Message(role="system", content=SUMMARY_SYSTEM_PROMPT))
    messages.append(Message(role="user", content=f"Query: {query}\n\nRetrieved abstracts:\n{stuffed}"))
    return messages


def _extract_citations(
    markdown: str,
    papers: list[ScoredPaper],
    *,
    provenance: Sequence[RecordProvenance] = (),
) -> list[SourcedCitation]:
    """Only enumerates the markers that are PRESENT.

    A sentence carrying no [N] produces nothing here and is invisible to
    `check_citations`, which is why `backend/app/verify/grounding.py` walks the
    summary text itself rather than this list.

    `source` and `retrieval_rank` are looked up BY PMID out of the retrieval
    provenance, not derived from `index`. The two agree today; deriving the
    rank from the marker would make them agree by construction and the field
    would then prove nothing about retrieval.
    """
    by_pmid = {p.pmid: p for p in provenance}
    indices = sorted({int(n) for n in re.findall(r"\[(\d+)\]", markdown)})
    out: list[SourcedCitation] = []
    for i in indices:
        if not 1 <= i <= len(papers):
            continue
        pmid = papers[i - 1].paper.pmid
        record = by_pmid.get(pmid)
        out.append(SourcedCitation(
            index=i,
            pmid=pmid,
            source=record.source if record else "",
            retrieval_rank=record.retrieval_rank if record else 0,
        ))
    return out


def generate_sourced_summary(
    llm: LLMPort,
    query: str,
    papers: list[ScoredPaper],
    *,
    distilled_context: str = "",
    provenance: Sequence[RecordProvenance] = (),
    request_id: str,
    session_id: str,
    user_id: str,
) -> SourcedSummary:
    if not papers:
        return SourcedSummary(markdown="", citations=[], degraded=False)

    result = llm.chat(
        _build_messages(query, papers, distilled_context),
        call_site="summary",
        request_id=request_id,
        session_id=session_id,
        user_id=user_id,
        json_schema=SUMMARY_JSON_SCHEMA,
    )
    if result.degraded:
        return SourcedSummary(markdown="", citations=[], degraded=True)

    # Cortex wraps JSON replies in markdown fences despite "ONLY valid JSON"
    # instructions; try_parse_json handles both raw JSON and fenced JSON
    # (see backend/app/loop/hyde.py and the branch-1 commit 4d894d3 postmortem).
    parsed = try_parse_json(result.content)
    try:
        markdown = parsed["summary_markdown"]
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("summary_markdown missing or empty")
    except (KeyError, ValueError, TypeError):
        return SourcedSummary(markdown="", citations=[], degraded=True)

    return SourcedSummary(
        markdown=markdown,
        citations=_extract_citations(markdown, papers, provenance=provenance),
        degraded=False,
    )
