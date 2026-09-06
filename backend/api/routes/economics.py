"""Card 1's /economics routes, implemented against the frozen response shapes
in plan-v2/00-SHARED-CONTRACTS.md section 4. Backed by NEULIT.CORE's cost
views (V_COST_BY_CALL_SITE, V_COST_PER_REQUEST, V_COST_BY_HOUR) and
CortexAnalyst for /economics/ask.
"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.api.contract_errors import UNPARSEABLE_BODY
from backend.api.limiter import limiter
from backend.snowflake.analyst import CortexAnalyst
from backend.snowflake.session import get_session, snowflake_available

logger = logging.getLogger("neulit.api.economics")

router = APIRouter(prefix="/economics")

_WINDOW_TO_HOURS = {"1h": 1, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}


class CallSiteBreakdown(BaseModel):
    call_site: str
    requests: int
    tokens: int
    cost_usd: float


class HourlyBreakdown(BaseModel):
    hour_iso: str
    tokens: int
    cost_usd: float


class EconomicsSummaryOut(BaseModel):
    total_requests: int
    total_tokens: int
    total_cost_usd: float
    by_call_site: list[CallSiteBreakdown]
    by_hour: list[HourlyBreakdown]


class RequestCallOut(BaseModel):
    call_site: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    degraded: bool


class EconomicsRequestOut(BaseModel):
    request_id: str
    calls: list[RequestCallOut]
    total_cost_usd: float


class EconomicsAskRequest(BaseModel):
    question: str


class EconomicsAskOut(BaseModel):
    answer: str
    sql: str
    rows: list[dict]


_EMPTY_SUMMARY = EconomicsSummaryOut(
    total_requests=0, total_tokens=0, total_cost_usd=0.0, by_call_site=[], by_hour=[]
)


@router.get("/summary", response_model=EconomicsSummaryOut)
@limiter.limit("20/minute")
def economics_summary(request: Request, window: str = "24h") -> EconomicsSummaryOut:
    if not snowflake_available():
        logger.warning("economics_summary: snowflake unavailable, returning empty summary")
        return _EMPTY_SUMMARY
    session = get_session()
    if session is None:
        return _EMPTY_SUMMARY

    hours = _WINDOW_TO_HOURS.get(window, 24)
    try:
        by_call_site_rows = session.sql(
            "SELECT CALL_SITE, CALLS, TOTAL_TOKENS, COST_USD FROM NEULIT.CORE.V_COST_BY_CALL_SITE"
        ).collect()
        by_hour_rows = session.sql(
            "SELECT HOUR, TOTAL_TOKENS, COST_USD FROM NEULIT.CORE.V_COST_BY_HOUR "
            f"WHERE HOUR >= DATEADD(hour, -{hours}, CURRENT_TIMESTAMP()) ORDER BY HOUR"
        ).collect()

        by_call_site = [
            CallSiteBreakdown(
                call_site=r["CALL_SITE"],
                requests=int(r["CALLS"] or 0),
                tokens=int(r["TOTAL_TOKENS"] or 0),
                cost_usd=float(r["COST_USD"] or 0.0),
            )
            for r in by_call_site_rows
        ]
        by_hour = [
            HourlyBreakdown(
                hour_iso=r["HOUR"].isoformat() if hasattr(r["HOUR"], "isoformat") else str(r["HOUR"]),
                tokens=int(r["TOTAL_TOKENS"] or 0),
                cost_usd=float(r["COST_USD"] or 0.0),
            )
            for r in by_hour_rows
        ]
        total_requests = sum(b.requests for b in by_call_site)
        total_tokens = sum(b.tokens for b in by_call_site)
        total_cost_usd = round(sum(b.cost_usd for b in by_call_site), 8)

        return EconomicsSummaryOut(
            total_requests=total_requests,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            by_call_site=by_call_site,
            by_hour=by_hour,
        )
    except Exception:
        logger.exception("economics_summary: query failed")
        return _EMPTY_SUMMARY


@router.get("/request/{request_id}", response_model=EconomicsRequestOut)
def economics_request(request_id: str) -> EconomicsRequestOut:
    empty = EconomicsRequestOut(request_id=request_id, calls=[], total_cost_usd=0.0)
    if not snowflake_available():
        return empty
    session = get_session()
    if session is None:
        return empty
    try:
        rows = session.sql(
            "SELECT CALL_SITE, PROMPT_TOKENS, COMPLETION_TOKENS, COST_USD, LATENCY_MS, DEGRADED "
            "FROM NEULIT.CORE.TOKEN_LEDGER WHERE REQUEST_ID = ? ORDER BY OCCURRED_AT",
            params=[request_id],
        ).collect()
        calls = [
            RequestCallOut(
                call_site=r["CALL_SITE"],
                prompt_tokens=int(r["PROMPT_TOKENS"] or 0),
                completion_tokens=int(r["COMPLETION_TOKENS"] or 0),
                cost_usd=float(r["COST_USD"] or 0.0),
                latency_ms=int(r["LATENCY_MS"] or 0),
                degraded=bool(r["DEGRADED"]),
            )
            for r in rows
        ]
        total_cost_usd = round(sum(c.cost_usd for c in calls), 8)
        return EconomicsRequestOut(request_id=request_id, calls=calls, total_cost_usd=total_cost_usd)
    except Exception:
        logger.exception("economics_request: query failed for request_id=%s", request_id)
        return empty


@router.post("/ask", response_model=EconomicsAskOut, responses=UNPARSEABLE_BODY)
@limiter.limit("6/minute")
def economics_ask(request: Request, body: EconomicsAskRequest) -> EconomicsAskOut:
    analyst = CortexAnalyst()
    result = analyst.ask(body.question)
    return EconomicsAskOut(**result)
