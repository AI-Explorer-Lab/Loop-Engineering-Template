"""Liveness and readiness endpoints."""

from fastapi import APIRouter, Response, status

from domain.res import HealthResponse
from service.health_service import get_liveness, get_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return await get_liveness()


@router.get("/ready", response_model=HealthResponse)
async def ready(response: Response) -> HealthResponse:
    result = await get_readiness()
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
