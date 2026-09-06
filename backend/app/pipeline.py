"""Orchestrates retrieval + loop + summary + verify + memory for POST /query,
against backend.contracts ports only. One function, one clear order, per
plan-v2/02-PHASE-CARD-2A-evermind-memory.md section 4.4.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from uuid import uuid4

from backend.app.corpus.conditions import CONDITIONS
from backend.app.corpus.retention import enforced as retention_enforced
from backend.app.llm.compress import compress_abstract
from backend.app.loop.hyde import run_hyde
from backend.app.loop.refine import run_refine
from backend.app.loop.relevance_check import run_relevance_check
from backend.app.loop.trace import LoopTraceEntry
from backend.app.retrieval.policy import RetrievalPolicy
from backend.app.summary.generate import SourcedCitation, generate_sourced_summary
from backend.app.verify.citation_check import check_citations
from backend.contracts.models import ChatResult, Message, ResearcherProfile, ScoredPaper
from backend.contracts.ports import LLMPort, MemoryPort
from backend.contracts.registry import get_services
from backend.memory.rerank import apply_memory

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2
RETRIEVAL_TOP_K = 10
SUMMARY_TOP_N = 5
MEMORY_BUDGET_SECONDS = 0.3


@dataclass
class MemoryInfo:
    applied: bool
    seen_filtered: int
    profile_used: bool
    distilled_context: str


@dataclass
class CallSiteCost:
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class CostInfo:
    total_tokens: int
    cost_usd: float
    by_call_site: dict[str, CallSiteCost]


@dataclass
class BrainRegionInfo:
    name: str
    atlas_label: str
    region_literature: str


@dataclass
class PolicyInfo:
    """What retrieval policy actually ran, and what it did to the prompt.

    Reported so a caller can see the breadth/depth trade it opted into rather
    than having to trust it happened. `prompt_tokens_before/after_compression`
    are measured on this request's own abstracts by the real compressor, not
    carried over from the gold-set bench.
    """

    label: str
    top_k: int
    compress_top_n: int
    papers_in_prompt: int
    prompt_tokens_before_compression: int
    prompt_tokens_after_compression: int


@dataclass
class QueryResult:
    request_id: str
    summary_markdown: str
    citations: list[SourcedCitation]
    papers: list[ScoredPaper]
    trace: list[LoopTraceEntry]
    region: BrainRegionInfo | None
    memory: MemoryInfo
    cost: CostInfo
    policy: PolicyInfo | None = None


class _RecordingLLM:
    """Wraps the real LLMPort so pipeline.py can aggregate cost from every
    ChatResult without every loop/summary/verify helper needing to return one.
    """

    def __init__(self, inner: LLMPort) -> None:
        self._inner = inner
        self.calls: list[tuple[str, ChatResult]] = []

    def chat(self, messages: list[Message], *, call_site, **kwargs) -> ChatResult:
        result = self._inner.chat(messages, call_site=call_site, **kwargs)
        self.calls.append((call_site, result))
        return result

    def health(self) -> dict:
        return self._inner.health()


def _load_memory_with_budget(
    memory: MemoryPort, user_id: str
) -> tuple[ResearcherProfile, set[str], bool]:
    """Bounds combined get_profile()+seen_pmids() latency to MEMORY_BUDGET_SECONDS.
    Returns empty defaults and applied=False on timeout or any failure.
    """
    default = (ResearcherProfile(user_id=user_id, specialty=None), set(), False)
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        profile_future = pool.submit(memory.get_profile, user_id)
        seen_future = pool.submit(memory.seen_pmids, user_id)
        # health() rides the same budget: get_profile/seen_pmids return an
        # empty default (not an error) when the backend is unset or
        # unreachable, so a fast "success" full of defaults would otherwise
        # be indistinguishable from a fast success full of real data. Only
        # health() tells us honestly whether personalization is real.
        health_future = pool.submit(memory.health)
        _, not_done = wait(
            [profile_future, seen_future, health_future], timeout=MEMORY_BUDGET_SECONDS
        )
        if not_done:
            return default
        profile, seen, health = profile_future.result(), seen_future.result(), health_future.result()
        if not health.get("ok", False):
            return default
        return profile, seen, True
    except Exception:
        return default
    finally:
        # ponytail: don't block on a hung memory backend past the budget -
        # a leaked thread finishes on its own and is GC'd, upgrade to a
        # cancellable client call if a real backend ever needs it.
        pool.shutdown(wait=False)


def _derive_region(papers: list[ScoredPaper]) -> BrainRegionInfo | None:
    if not papers:
        return None
    condition = next((c for c in CONDITIONS if c.name == papers[0].paper.condition), None)
    if condition is None:
        return None
    return BrainRegionInfo(
        name=condition.name,
        atlas_label=condition.atlas_label,
        region_literature=condition.region_literature,
    )


def _aggregate_cost(calls: list[tuple[str, ChatResult]]) -> CostInfo:
    by_call_site: dict[str, CallSiteCost] = {}
    total_tokens = 0
    total_cost = 0.0
    for call_site, result in calls:
        total_tokens += result.usage.total_tokens
        total_cost += result.usage.cost_usd
        entry = by_call_site.setdefault(call_site, CallSiteCost())
        entry.tokens += result.usage.total_tokens
        entry.cost_usd += result.usage.cost_usd
    return CostInfo(total_tokens=total_tokens, cost_usd=round(total_cost, 8), by_call_site=by_call_site)


def _compress_for_policy(
    query: str,
    papers: list[ScoredPaper],
    policy: RetrievalPolicy,
    secondary_query: str | None = None,
) -> tuple[list[ScoredPaper], int, int]:
    """Return `papers` with each abstract extractively compressed to
    `policy.compress_top_n` sentences, plus the before/after token totals.

    `Paper` is frozen, so this rebuilds each one via `dataclasses.replace`
    rather than mutating in place -- the originals stay intact for anything
    holding a reference (the citation checker still needs to verify claims
    against the *uncompressed* source, which is why compression is applied to
    the summary input only, at the call site, and not to `reranked` globally).
    """
    compressed: list[ScoredPaper] = []
    tokens_before = tokens_after = 0
    for sp in papers:
        result = compress_abstract(
            query, sp.paper.abstract,
            top_n=policy.compress_top_n,
            secondary_query=secondary_query,
        )
        tokens_before += result.tokens_before
        tokens_after += result.tokens_after
        compressed.append(replace(sp, paper=replace(sp.paper, abstract=result.text)))
    return compressed, tokens_before, tokens_after


def _emit(on_stage: Callable[[str, dict], None] | None, stage: str, **detail) -> None:
    if on_stage is not None:
        on_stage(stage, detail)


def run_query(
    query: str,
    user_id: str,
    session_id: str,
    personalize: bool,
    *,
    on_stage: Callable[[str, dict], None] | None = None,
    policy: RetrievalPolicy | None = None,
) -> QueryResult:
    """`policy=None` is today's exact behaviour: retrieve `RETRIEVAL_TOP_K`,
    summarise the top `SUMMARY_TOP_N` uncompressed abstracts. Passing a
    `RetrievalPolicy` opts into the breadth/depth trade measured in
    `backend/measurement/run_policy_bench.py` -- retrieve `policy.top_k` and
    compress each abstract to `policy.compress_top_n` sentences before the
    summary call.

    Opt-in rather than default on purpose: the bench measures what reaches the
    prompt, not answer quality (no LLM is called in it), so switching the live
    default is a decision that needs a live citation-checked run behind it, not
    a retrieval metric.
    """
    request_id = str(uuid4())
    services = get_services()
    llm = _RecordingLLM(services.llm)
    # A deleted or redacted document must not reach a summary, a citation, or
    # the response. Enforced here rather than trusted to the store, because
    # the Cortex Search index is a materialisation with a one-hour lag and can
    # legitimately still hold a record that PAPERS no longer does. Returns the
    # port unchanged when no tombstone exists.
    retrieval = retention_enforced(services.retrieval)

    if personalize:
        profile, seen, memory_read_ok = _load_memory_with_budget(services.memory, user_id)
    else:
        profile, seen, memory_read_ok = ResearcherProfile(user_id=user_id, specialty=None), set(), False
    memory_applied = personalize and memory_read_ok

    current_query = query
    hyde_query = run_hyde(llm, current_query, request_id=request_id, session_id=session_id, user_id=user_id)
    _emit(on_stage, "hyde_expand", iteration=1)

    trace: list[LoopTraceEntry] = []
    reranked: list[ScoredPaper] = []
    total_demoted = 0

    for round_idx in range(1, MAX_ROUNDS + 1):
        raw_results = retrieval.search(
            current_query,
            secondary_query=hyde_query,
            top_k=policy.top_k if policy else RETRIEVAL_TOP_K,
            exclude_pmids=tuple(seen) if memory_applied else (),
        )
        if memory_applied:
            reranked, demoted = apply_memory(raw_results, profile, seen)
        else:
            reranked, demoted = raw_results, 0
        total_demoted += demoted
        _emit(on_stage, "retrieval", iteration=round_idx)

        verdict = run_relevance_check(
            llm, current_query, reranked, request_id=request_id, session_id=session_id, user_id=user_id
        )
        _emit(on_stage, "relevance_check", iteration=round_idx)
        trace.append(LoopTraceEntry(
            iteration=round_idx,
            retrieved_pmids=[sp.paper.pmid for sp in reranked],
            relevant=verdict["relevant"],
            confidence=verdict["confidence"],
            note=verdict["note"],
            memory_applied=memory_applied,
            seen_filtered=demoted,
        ))

        if verdict["relevant"] or round_idx == MAX_ROUNDS:
            break

        current_query = run_refine(
            llm, current_query, verdict["note"], request_id=request_id, session_id=session_id, user_id=user_id
        )
        _emit(on_stage, "refine_query", iteration=round_idx)
        hyde_query = run_hyde(llm, current_query, request_id=request_id, session_id=session_id, user_id=user_id)
        _emit(on_stage, "hyde_expand", iteration=round_idx + 1)

    # Under a policy the whole retrieved set is the point -- cutting to
    # SUMMARY_TOP_N would throw away exactly the breadth the policy bought.
    top_papers = reranked[: policy.top_k] if policy else reranked[:SUMMARY_TOP_N]

    # Compression feeds the summary call only. `top_papers` (uncompressed) is
    # what goes to check_citations and into the response, so claims are still
    # verified against, and the user still reads, the real abstract.
    summary_papers = top_papers
    policy_info: PolicyInfo | None = None
    if policy:
        summary_papers, tokens_before, tokens_after = _compress_for_policy(
            query, top_papers, policy, hyde_query
        )
        policy_info = PolicyInfo(
            label=policy.label,
            top_k=policy.top_k,
            compress_top_n=policy.compress_top_n,
            papers_in_prompt=len(summary_papers),
            prompt_tokens_before_compression=tokens_before,
            prompt_tokens_after_compression=tokens_after,
        )

    distilled_context = profile.distilled_context if memory_applied else ""
    summary = generate_sourced_summary(
        llm, query, summary_papers,
        distilled_context=distilled_context,
        request_id=request_id, session_id=session_id, user_id=user_id,
    )
    _emit(on_stage, "summarize")
    citations = check_citations(
        llm, query, summary.markdown, top_papers, summary.citations,
        request_id=request_id, session_id=session_id, user_id=user_id,
    )
    _emit(on_stage, "citation_check")

    if personalize:
        matched_conditions = sorted({sp.paper.condition for sp in top_papers})
        try:
            services.memory.record_query(user_id, session_id, query, matched_conditions)
            if top_papers:
                services.memory.record_papers_shown(
                    user_id, session_id, [sp.paper.pmid for sp in top_papers]
                )
        except Exception:
            # MemoryPort implementations must degrade rather than raise, but a
            # search must survive even a port that breaks that contract.
            logger.warning("Memory write failed; continuing without persisting this query", exc_info=True)

    return QueryResult(
        request_id=request_id,
        summary_markdown=summary.markdown,
        citations=citations,
        papers=top_papers,
        trace=trace,
        region=_derive_region(top_papers),
        memory=MemoryInfo(
            applied=memory_applied,
            seen_filtered=total_demoted,
            profile_used=memory_applied,
            distilled_context=distilled_context,
        ),
        cost=_aggregate_cost(llm.calls),
        policy=policy_info,
    )
