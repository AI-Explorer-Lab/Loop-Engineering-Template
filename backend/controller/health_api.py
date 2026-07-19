from fastapi import APIRouter, Request

from ..constant.values import APP_VERSION
from ..domain.res import ApiResponse, HealthData


router = APIRouter(tags=["health"])


@router.get("/health", response_model=ApiResponse[HealthData])
async def health(request: Request) -> ApiResponse[HealthData]:
    environment = request.app.state.environment
    return ApiResponse(
        data=HealthData(
            status="ok",
            environment=environment,
            version=APP_VERSION,
        ),
        request_id=getattr(request.state, "request_id", None),
    )
