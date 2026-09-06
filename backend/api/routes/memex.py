"""MemEx routes. ADDITIVE - no existing route changes behaviour.

--- WHY THE RESPONSE HAS DUPLICATE-LOOKING KEYS -----------------------------

There are two live contracts in this repo and they disagree:

  A. plan-v2/00-SHARED-CONTRACTS.md §4 - the frozen target shape:
     { request_id, summary_markdown, citations, papers, trace, region,
       memory{...}, cost{...} }
  B. frontend/src/lib/api.ts `QueryResult` - what the Next app ACTUALLY
     parses today:
     { summary_text, citations[{marker,pmid,title,condition}], trace,
       low_confidence, degraded, no_match, too_generic, example_query,
       suggested_conditions, flagged_claims, case_context, differential }

Rewriting the frontend hours before a demo is not a trade worth making, and
neither is missing the frozen contract. So `/memex/query` returns a SUPERSET
that satisfies both: every §4 key, every api.ts key, and citation objects
carrying the union of both field sets. TypeScript is structurally typed, so
extra fields are free on the frontend side.

`POST /query` is untouched. This router only adds paths under /memex.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, StrictBool

from backend.api.contract_errors import UNPARSEABLE_BODY
from backend.memex import pricing
from backend.memex.engine import ask
from backend.memex.market import get_market
from backend.memex.scarcity import get_index

router = APIRouter(prefix="/memex", tags=["memex"])


class MemexQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    session_id: str = "demo-session"
    user_id: str = "demo-researcher"
    # StrictBool for the same reason as QueryRequest.personalize: plain `bool` accepts
    # `0` and `1`, so `{"settle": 0}` booked no trade while the contract declared a
    # boolean and the service answered 200. See backend/api/schemas.py.
    personalize: StrictBool = True
    #: Book a trade for this question. False lets the UI price without settling.
    settle: StrictBool = True


class ShockRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    session_id: str = "demo-session"
    user_id: str = "demo-researcher"


def _paper_dto(scored: Any, index: int) -> dict:
    """ScoredPaperDTO (§4) plus the snake_case fields api.ts reads."""
    p = scored.paper
    return {
        "index": index,
        "pmid": p.pmid,
        "title": p.title,
        "abstract": p.abstract,
        "journal": p.journal,
        "year": p.year,
        "condition": p.condition,
        "isRare": p.is_rare,
        "is_rare": p.is_rare,
        "rarity": "rare" if p.is_rare else "common",
        "url": p.url,
        "score": scored.score,
        "lexicalScore": scored.lexical_score,
        "semanticScore": scored.semantic_score,
        "rarityMultiplier": scored.rarity_multiplier,
        "memoryMultiplier": scored.memory_multiplier,
    }


def _citation_dto(scored: Any, index: int) -> dict:
    """Union of §4's Citation and api.ts's Citation. Both consumers are happy."""
    return {
        "index": index,
        "marker": f"[{index}]",
        "pmid": scored.paper.pmid,
        "title": scored.paper.title,
        "condition": scored.paper.condition,
        "supported": True,
        "note": None,
    }


def _to_response(result: Any, trade: Any | None) -> dict:
    papers = [_paper_dto(sp, i) for i, sp in enumerate(result.papers, start=1)]
    citations = [_citation_dto(sp, i) for i, sp in enumerate(result.papers, start=1)]
    trace = [
        {
            "iteration": 1,
            "retrieved_pmids": [sp.paper.pmid for sp in result.papers],
            "relevant": True,
            "confidence": round(min(1.0, result.papers[0].score / 20.0), 3)
            if result.papers
            else 0.0,
            "note": f"token-overlap retrieval, rarity boost applied, {len(result.papers)} kept",
            "relevant_count": len(result.papers),
            "total_count": len(result.papers),
        }
    ]

    by_call_site = {
        "summary": {
            "tokens": result.cold.total_tokens + result.warm.total_tokens,
            "cost_usd": result.cold.usd + result.warm.usd,
        }
    }

    degraded = result.cold.degraded or result.warm.degraded
    payload = {
        # --- §4 keys -------------------------------------------------------
        "request_id": result.request_id,
        "summary_markdown": result.warm.summary_markdown,
        "citations": citations,
        "papers": papers,
        "trace": trace,
        "region": result.papers[0].paper.condition if result.papers else None,
        "memory": result.as_dict()["memory"],
        "cost": {
            "total_tokens": result.cold.total_tokens + result.warm.total_tokens,
            "cost_usd": result.cold.usd + result.warm.usd,
            "by_call_site": by_call_site,
        },
        # --- frontend/src/lib/api.ts QueryResult keys ----------------------
        "summary_text": result.warm.summary_markdown,
        "low_confidence": False,
        "degraded": degraded,
        "no_match": not result.papers,
        "too_generic": False,
        "example_query": None,
        "suggested_conditions": [],
        "flagged_claims": [],
        "case_context": None,
        "differential": [],
        # --- MemEx: the two-path economics ---------------------------------
        "cold": result.cold.as_dict(),
        "warm": result.warm.as_dict(),
        "delta": result.as_dict()["delta"],
        "scarcity": result.as_dict()["scarcity"],
        "trade": trade.as_dict() if trade is not None else None,
    }
    return payload


@router.post("/query", responses=UNPARSEABLE_BODY)
def memex_query(payload: MemexQueryRequest) -> dict:
    """One question, both paths, priced. Optionally books the trade."""
    result = ask(
        payload.query,
        user_id=payload.user_id,
        session_id=payload.session_id,
        personalize=payload.personalize,
    )
    trade = get_market().settle_question(result) if payload.settle else None
    return _to_response(result, trade)


@router.get("/market")
def market_book() -> dict:
    """Wallets, trade feed, totals. What the dashboard polls."""
    return get_market().book()


@router.post("/market/reset")
def market_reset() -> dict:
    """Back to opening balances. The rehearsal button."""
    get_market().reset()
    return {"reset": True, **get_market().book()}


@router.post("/shock", responses=UNPARSEABLE_BODY)
def shock(payload: ShockRequest) -> dict:
    """Destroy the memory, re-price the same question, return the spike.

    The forget() lands first, so the re-priced run genuinely has no profile to
    lean on - the spike is measured, not asserted.
    """
    from backend.contracts.registry import get_services

    market = get_market()
    before = ask(
        payload.query,
        user_id=payload.user_id,
        session_id=payload.session_id,
        personalize=True,
    )
    try:
        get_services().memory.forget(payload.user_id)
        destroyed = 1
    except Exception:  # noqa: BLE001 - a demo beat must not 500
        destroyed = 0

    after = ask(
        payload.query,
        user_id=payload.user_id,
        session_id=payload.session_id,
        personalize=False,
    )
    event = market.run_shock(before, destroyed=destroyed)
    return {
        "shock": event.as_dict(),
        "before": _to_response(before, None),
        "after": _to_response(after, None),
        "market": market.book(),
    }


@router.get("/scarcity")
def scarcity_table() -> dict:
    """Every condition, its paper count, and its multiplier. Rarest first."""
    index = get_index()
    return {
        "total_papers": index.total_papers,
        "max_condition_papers": index.max_condition_papers,
        "formula": "ln(N / n_c) / ln(N / n_max)",
        "conditions": index.table(),
    }


@router.get("/pricing")
def pricing_table() -> dict:
    """The published rate table, so the cost claim is inspectable on stage."""
    return {
        "source": "Snowflake Service Consumption Table, Table 6(a), eff. 2026-07-31",
        "url": "https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf",
        "usd_per_credit": pricing.usd_per_credit(),
        "cold_model": pricing.cold_model(),
        "warm_model": pricing.warm_model(),
        "rates": {
            model: {
                "input_per_mtok": rate.input,
                "output_per_mtok": rate.output,
                "availability": rate.availability,
                "note": rate.note,
            }
            for model, rate in pricing.CREDIT_RATES.items()
        },
    }


@router.get("/health")
def memex_health() -> dict:
    from backend.contracts.registry import get_services

    services = get_services()
    return {
        "status": "ok",
        "scarcity": get_index().health(),
        "ports": {
            "retrieval": services.retrieval.health(),
            "llm": services.llm.health(),
            "memory": services.memory.health(),
            "ledger": services.ledger.health(),
        },
    }
