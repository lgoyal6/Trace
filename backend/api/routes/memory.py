from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from backend.api.contract_errors import UNPARSEABLE_BODY
from backend.api.dependencies import get_services_dep
from backend.api.limiter import limiter
from backend.api.schemas import ForgetRequest, ProfileOut, SpecialtyRequest, ThreadOut
from backend.contracts.registry import Services

router = APIRouter(prefix="/memory")


@router.get("/profile", response_model=ProfileOut)
@limiter.limit("30/minute")
def memory_profile(
    request: Request, user_id: str, services: Services = Depends(get_services_dep)
) -> ProfileOut:
    profile = services.memory.get_profile(user_id)
    seen_pmid_count = len(services.memory.seen_pmids(user_id))
    return ProfileOut(
        user_id=profile.user_id,
        specialty=profile.specialty,
        conditions_explored=profile.conditions_explored,
        query_count=profile.query_count,
        distilled_context=profile.distilled_context,
        seen_pmid_count=seen_pmid_count,
    )


@router.post("/specialty", status_code=204, responses=UNPARSEABLE_BODY)
@limiter.limit("30/minute")
def memory_specialty(
    request: Request, body: SpecialtyRequest, services: Services = Depends(get_services_dep)
) -> None:
    services.memory.set_specialty(body.user_id, body.specialty)


@router.post("/forget", status_code=204, responses=UNPARSEABLE_BODY)
@limiter.limit("5/minute")
def memory_forget(
    request: Request, body: ForgetRequest, services: Services = Depends(get_services_dep)
) -> None:
    services.memory.forget(body.user_id)


@router.get("/thread", response_model=ThreadOut)
@limiter.limit("30/minute")
def memory_thread(
    request: Request, user_id: str, session_id: str, services: Services = Depends(get_services_dep)
) -> ThreadOut:
    thread = services.memory.get_thread(user_id, session_id)
    return ThreadOut(
        session_id=thread.session_id,
        user_id=thread.user_id,
        queries=thread.queries,
        pmids_shown=thread.pmids_shown,
    )
