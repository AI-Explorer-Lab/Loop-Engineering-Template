from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config.config import (
    repo_root_from_settings,
    settings,
    validate_settings,
)
from .constant.values import API_PREFIX, APP_NAME, APP_VERSION
from .controller.health_api import router as health_router
from .controller.platform_api import router as platform_router
from .controller.plan_api import router as plan_router
from .controller.queue_api import router as queue_router
from .controller.task_api import router as task_router
from .exceptions.exception_handler import register_exception_handlers
from .middlewares.request_logging import RequestLoggingMiddleware
from .service.queue_service import QueueService
from .service.project_registry import ProjectRegistry
from .service.platform_service import PlatformService
from .service.task_service import TaskService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def create_app(
    *,
    config: Any = settings,
    task_service: TaskService | None = None,
    queue_service: QueueService | None = None,
    validate_config: bool = True,
) -> FastAPI:
    owns_service = task_service is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if validate_config:
            validate_settings(config)

        environment = config.get("environment", {}) or {}
        agent = config.get("agent", {}) or {}
        registry: ProjectRegistry | None = None
        if task_service is None and queue_service is None:
            registry = ProjectRegistry(config)
            service = registry.default.task_service
            queues = registry.default.queue_service
        else:
            service = task_service or TaskService(
                repo_root_from_settings(config),
                validation_timeout_seconds=float(
                    agent.get("validation_timeout_seconds", 900)
                ),
            )
            queues = queue_service
            if queues is None and isinstance(service, TaskService):
                queues = QueueService(
                    repo_root_from_settings(config),
                    validation_timeout_seconds=float(
                        agent.get("validation_timeout_seconds", 900)
                    ),
                    executor=service.executor,
                )
        app.state.environment = str(environment.get("name", "development"))
        app.state.project_registry = registry
        app.state.platform_service = (
            PlatformService(registry, config) if registry is not None else None
        )
        app.state.task_service = service
        app.state.queue_service = queues
        try:
            yield
        finally:
            if registry is not None:
                registry.close(wait=False)
            elif owns_service:
                service.close(wait=False)

    environment = config.get("environment", {}) or {}
    server = config.get("server", {}) or {}
    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        debug=bool(environment.get("debug", False)),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in server.get("cors_origins", [])],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "X-Project-ID"],
    )
    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(platform_router, prefix=API_PREFIX)
    app.include_router(task_router, prefix=API_PREFIX)
    app.include_router(queue_router, prefix=API_PREFIX)
    app.include_router(plan_router, prefix=API_PREFIX)
    return app


app = create_app()
