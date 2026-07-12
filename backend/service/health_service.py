"""Health and readiness use cases."""

from config.config import settings
from database.lifecycle import check_database
from domain.res import HealthResponse


async def get_liveness() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=str(settings.get("app.name")),
        version=str(settings.get("app.version")),
    )


async def get_readiness() -> HealthResponse:
    database_ready = await check_database()
    return HealthResponse(
        status="ok" if database_ready else "unavailable",
        service=str(settings.get("app.name")),
        version=str(settings.get("app.version")),
        checks={"database": "ok" if database_ready else "unavailable"},
    )
