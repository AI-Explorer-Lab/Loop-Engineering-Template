"""FastAPI composition root."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config.config import settings, validate_settings
from controller.health_api import router as health_router
from database.lifecycle import close_database, init_database
from exceptions.exception_handler import register_exception_handlers
from middlewares.request_logging import RequestLoggingMiddleware

logging.basicConfig(
    level=str(settings.get("environment.log_level", "INFO")),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    validate_settings()
    await init_database()
    try:
        yield
    finally:
        await close_database()


def create_app() -> FastAPI:
    application = FastAPI(
        title=str(settings.get("app.name")),
        version=str(settings.get("app.version")),
        lifespan=lifespan,
    )
    application.add_middleware(RequestLoggingMiddleware)
    application.include_router(health_router)
    register_exception_handlers(application)
    return application


app = create_app()


def run() -> None:
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
