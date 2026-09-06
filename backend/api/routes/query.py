from __future__ import annotations

import json
import logging
import queue
import threading
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from backend.api.limiter import limiter
from backend.api.schemas import (
    BrainRegionOut,
    CallSiteCostOut,
    CitationOut,
    ClaimVerdictOut,
    CostOut,
    GroundingOut,
    MemoryOut,
    PaperOut,
    PolicyOut,
    QueryRequest,
    QueryResponse,
    RecordProvenanceOut,
    ScoredPaperOut,
    TraceRoundOut,
)
from backend.app.pipeline import QueryResult, run_query
from backend.app.retrieval.policy import policy_for_label

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_response(result: QueryResult) -> QueryResponse:
    return QueryResponse(
        request_id=result.request_id,
        summary_markdown=result.summary_markdown,
        citations=[
            CitationOut(
                index=c.index,
                pmid=c.pmid,
                supported=c.supported,
                note=c.note,
                source=c.source,
                retrieval_rank=c.retrieval_rank,
            )
            for c in result.citations
        ],
        papers=[
            ScoredPaperOut(
                paper=PaperOut(
                    pmid=sp.paper.pmid,
                    title=sp.paper.title,
                    abstract=sp.paper.abstract,
                    journal=sp.paper.journal,
                    year=sp.paper.year,
                    condition=sp.paper.condition,
                    is_rare=sp.paper.is_rare,
                    url=sp.paper.url,
                ),
                score=sp.score,
                lexical_score=sp.lexical_score,
                semantic_score=sp.semantic_score,
                rarity_multiplier=sp.rarity_multiplier,
                memory_multiplier=sp.memory_multiplier,
            )
            for sp in result.papers
        ],
        trace=[
            TraceRoundOut(
                iteration=t.iteration,
                retrieved_pmids=t.retrieved_pmids,
                relevant=t.relevant,
                confidence=t.confidence,
                note=t.note,
                memory_applied=t.memory_applied,
                seen_filtered=t.seen_filtered,
            )
            for t in result.trace
        ],
        region=(
            BrainRegionOut(
                name=result.region.name,
                atlas_label=result.region.atlas_label,
                region_literature=result.region.region_literature,
            )
            if result.region is not None
            else None
        ),
        memory=MemoryOut(
            applied=result.memory.applied,
            seen_filtered=result.memory.seen_filtered,
            profile_used=result.memory.profile_used,
            distilled_context=result.memory.distilled_context,
        ),
        cost=CostOut(
            total_tokens=result.cost.total_tokens,
            cost_usd=result.cost.cost_usd,
            by_call_site={
                site: CallSiteCostOut(tokens=c.tokens, cost_usd=c.cost_usd)
                for site, c in result.cost.by_call_site.items()
            },
        ),
        policy=_policy_out(result),
        grounding=_grounding_out(result),
        retrieval_provenance=[
            RecordProvenanceOut(
                pmid=r.pmid, source=r.source, retrieval_rank=r.retrieval_rank, score=r.score
            )
            for r in result.retrieval_provenance
        ],
        abstained=result.abstained,
    )


def _grounding_out(result: QueryResult) -> GroundingOut | None:
    g = result.grounding
    if g is None:
        return None
    return GroundingOut(
        **g.as_dict(),
        claims=[
            ClaimVerdictOut(
                position=c.position,
                text=c.text,
                cited_indices=list(c.cited_indices),
                cited_pmids=list(c.cited_pmids),
                verdict=c.verdict,
                best_overlap=c.best_overlap,
                numeric_conflict=c.numeric_conflict,
            )
            for c in g.claims
        ],
    )


def _policy_out(result: QueryResult) -> PolicyOut | None:
    p = result.policy
    if p is None:
        return None
    before, after = p.prompt_tokens_before_compression, p.prompt_tokens_after_compression
    saved = max(0, before - after)
    return PolicyOut(
        label=p.label,
        top_k=p.top_k,
        compress_top_n=p.compress_top_n,
        papers_in_prompt=p.papers_in_prompt,
        prompt_tokens_before_compression=before,
        prompt_tokens_after_compression=after,
        tokens_saved=saved,
        reduction_pct=round(100.0 * saved / before, 2) if before else 0.0,
    )


def _request_scoped_ids(request: Request) -> tuple[str, str, str]:
    request_id = str(uuid.uuid4())
    session_id = request.headers.get("x-session-id") or request_id
    user_id = request.headers.get("x-user-id") or "anonymous"
    return request_id, session_id, user_id


@router.post("/query", response_model=QueryResponse)
@limiter.limit("10/minute")
def query(request: Request, payload: QueryRequest) -> QueryResponse:
    result = run_query(
        payload.query, payload.user_id, payload.session_id, payload.personalize,
        policy=policy_for_label(payload.policy) if payload.policy else None,
    )
    return _to_response(result)


@router.post("/query/stream")
@limiter.limit("10/minute")
def query_stream(request: Request, payload: QueryRequest) -> StreamingResponse:
    """Runs the same pipeline as POST /query but over SSE, emitting `stage`
    events as the search loop progresses (hyde_expand, retrieval,
    relevance_check, refine_query, summarize, citation_check - the same
    stage names v1 used) followed by one `done` event with the full result.
    The frozen HTTP contract in plan-v2/00-SHARED-CONTRACTS.md section 4
    doesn't define this route's event shape, so it's ours to choose; matching
    v1's stage names is what frontend/src/components/progress-timeline.tsx
    already expects.
    """
    event_queue: queue.Queue = queue.Queue()

    def on_stage(stage: str, detail: dict) -> None:
        event_queue.put({"type": "stage", "stage": stage, **detail})

    def worker() -> None:
        try:
            result = _to_response(run_query(
                payload.query, payload.user_id, payload.session_id, payload.personalize,
                on_stage=on_stage,
                policy=policy_for_label(payload.policy) if payload.policy else None,
            ))
            # by_alias=True or this diverges from POST /query. The response
            # models are CamelModel, so `response_model` serialization on the
            # non-streaming route emits camelCase, while a bare model_dump()
            # here emitted snake_case -- meaning the SSE path (the one the UI
            # actually uses) was shipping `memory_multiplier`/`is_rare` where
            # generated api-types.ts expects `memoryMultiplier`/`isRare`.
            # profile-panel.tsx's cold-vs-warm comparison divides by
            # `memoryMultiplier`, so it was silently computing NaN.
            event_queue.put({"type": "done", "result": result.model_dump(by_alias=True)})
        except Exception:
            logger.exception("Unhandled error in /query/stream pipeline")
            event_queue.put({"type": "error", "message": "Internal error while processing query."})
        finally:
            event_queue.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def event_generator():
        while True:
            item = event_queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
