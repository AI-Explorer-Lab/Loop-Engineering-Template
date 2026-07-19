from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from starlette.concurrency import run_in_threadpool

from ..domain.req import PlanConfirmRequest, PlanCreateRequest
from ..domain.res import ApiResponse
from ..exceptions.business_exception import BusinessException
from ..service.plan_service import PlanService
from .dependencies import project_context


router = APIRouter(prefix="/plans", tags=["plans"])


def _service(request: Request) -> PlanService:
    context = project_context(request)
    service = None if context is None else context.plan_service
    if service is None:
        raise BusinessException("Automatic planning is unavailable", status_code=503)
    return service


@router.post("", response_model=ApiResponse[dict[str, Any]])
async def create_plan(
    payload: PlanCreateRequest, request: Request
) -> ApiResponse[dict[str, Any]]:
    return ApiResponse(
        data=await run_in_threadpool(
            _service(request).create,
            name=payload.name,
            requirement=payload.requirement,
            acceptance_criteria=payload.acceptance_criteria,
        )
    )


@router.get("/{plan_id}", response_model=ApiResponse[dict[str, Any]])
async def get_plan(plan_id: str, request: Request) -> ApiResponse[dict[str, Any]]:
    return ApiResponse(data=_service(request).get(plan_id))


@router.post(
    "/{plan_id}/confirm",
    response_model=ApiResponse[dict[str, Any]],
    status_code=status.HTTP_202_ACCEPTED,
)
async def confirm_plan(
    plan_id: str,
    payload: PlanConfirmRequest,
    request: Request,
) -> ApiResponse[dict[str, Any]]:
    return ApiResponse(
        data=await run_in_threadpool(
            _service(request).confirm,
            plan_id,
            reviewer=payload.reviewer,
            edited_draft=payload.edited_draft,
        )
    )


__all__ = ["router"]
