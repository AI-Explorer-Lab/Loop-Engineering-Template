from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..domain.res import (
    ApiResponse,
    EventPageData,
    HistoryPageData,
    LogData,
    NotificationData,
    NotificationSettingsData,
    ProjectData,
)
from ..domain.req import NotificationSettingsRequest
from ..exceptions.business_exception import BusinessException
from ..service.platform_service import PlatformService


router = APIRouter(tags=["platform"])


def _service(request: Request) -> PlatformService:
    service: PlatformService | None = getattr(
        request.app.state, "platform_service", None
    )
    if service is None:
        raise BusinessException("Platform service is unavailable", status_code=503)
    return service


def _project_id(
    service: PlatformService,
    header_value: str | None,
    query_value: str | None = None,
) -> str:
    return service.registry.get(query_value or header_value).project_id


@router.get("/projects", response_model=ApiResponse[list[ProjectData]])
async def list_projects(request: Request) -> ApiResponse[list[ProjectData]]:
    return ApiResponse(data=_service(request).projects())


@router.get("/capabilities", response_model=ApiResponse[dict])
async def capabilities(
    request: Request,
    project_id: str | None = None,
    x_project_id: str | None = Header(default=None),
) -> ApiResponse[dict]:
    service = _service(request)
    resolved = _project_id(service, x_project_id, project_id)
    return ApiResponse(
        data=await run_in_threadpool(service.capabilities, resolved)
    )


@router.get("/metrics", response_model=ApiResponse[dict])
async def metrics(
    request: Request,
    project_id: str | None = None,
    x_project_id: str | None = Header(default=None),
) -> ApiResponse[dict]:
    service = _service(request)
    resolved = _project_id(service, x_project_id, project_id)
    return ApiResponse(data=service.metrics(resolved))


@router.get("/history", response_model=ApiResponse[HistoryPageData])
async def list_history(
    request: Request,
    project_id: str | None = None,
    kind: str | None = Query(default=None, pattern="^(task|queue)$"),
    status: str | None = None,
    query: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ApiResponse[HistoryPageData]:
    data = _service(request).history(
        project_id=project_id,
        kind=kind,
        status=status,
        query=query,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(data=HistoryPageData.model_validate(data))


@router.get(
    "/history/{kind}/{identifier}/events",
    response_model=ApiResponse[EventPageData],
)
async def list_events(
    kind: str,
    identifier: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    project_id: str | None = None,
    x_project_id: str | None = Header(default=None),
) -> ApiResponse[EventPageData]:
    service = _service(request)
    data = service.events(
        _project_id(service, x_project_id, project_id),
        kind,
        identifier,
        after=after,
        limit=limit,
    )
    return ApiResponse(data=EventPageData.model_validate(data))


@router.get("/history/{kind}/{identifier}/stream")
async def stream_events(
    kind: str,
    identifier: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    project_id: str | None = None,
    x_project_id: str | None = Header(default=None),
) -> StreamingResponse:
    service = _service(request)
    resolved_project_id = _project_id(service, x_project_id, project_id)

    async def generate():
        cursor = after
        while True:
            if await request.is_disconnected():
                return
            page = service.events(
                resolved_project_id,
                kind,
                identifier,
                after=cursor,
                limit=200,
            )
            for event in page["items"]:
                cursor = int(event.get("seq", cursor))
                yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if page["terminal"] and not page["items"]:
                yield "event: end\ndata: {}\n\n"
                return
            if not page["items"]:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get(
    "/history/{kind}/{identifier}/logs",
    response_model=ApiResponse[list[LogData]],
)
async def list_logs(
    kind: str,
    identifier: str,
    request: Request,
    x_project_id: str | None = Header(default=None),
) -> ApiResponse[list[LogData]]:
    service = _service(request)
    return ApiResponse(
        data=service.logs(_project_id(service, x_project_id), kind, identifier)
    )


@router.get(
    "/history/{kind}/{identifier}/logs/{log_id:path}",
    response_class=PlainTextResponse,
)
async def read_log(
    kind: str,
    identifier: str,
    log_id: str,
    request: Request,
    x_project_id: str | None = Header(default=None),
) -> PlainTextResponse:
    service = _service(request)
    content = service.read_log(
        _project_id(service, x_project_id), kind, identifier, log_id
    )
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@router.get(
    "/notifications",
    response_model=ApiResponse[list[NotificationData]],
)
async def list_notifications(
    request: Request,
    project_id: str | None = None,
    unread_only: bool = False,
) -> ApiResponse[list[NotificationData]]:
    return ApiResponse(
        data=_service(request).notifications(
            project_id=project_id,
            unread_only=unread_only,
        )
    )


@router.post(
    "/notifications/{notification_id}/read",
    response_model=ApiResponse[NotificationData],
)
async def mark_notification_read(
    notification_id: str,
    request: Request,
    x_project_id: str | None = Header(default=None),
) -> ApiResponse[NotificationData]:
    service = _service(request)
    return ApiResponse(
        data=service.mark_notification_read(
            _project_id(service, x_project_id), notification_id
        )
    )


@router.get(
    "/notifications/settings",
    response_model=ApiResponse[NotificationSettingsData],
)
async def notification_settings(
    request: Request,
    project_id: str | None = None,
    x_project_id: str | None = Header(default=None),
) -> ApiResponse[NotificationSettingsData]:
    service = _service(request)
    return ApiResponse(
        data=service.notification_settings_for(
            _project_id(service, x_project_id, project_id)
        )
    )


@router.post(
    "/notifications/settings",
    response_model=ApiResponse[NotificationSettingsData],
)
async def update_notification_settings(
    payload: NotificationSettingsRequest,
    request: Request,
    project_id: str | None = None,
    x_project_id: str | None = Header(default=None),
) -> ApiResponse[NotificationSettingsData]:
    service = _service(request)
    return ApiResponse(
        data=service.update_notification_settings(
            _project_id(service, x_project_id, project_id),
            in_app=payload.in_app,
            browser=payload.browser,
        )
    )
