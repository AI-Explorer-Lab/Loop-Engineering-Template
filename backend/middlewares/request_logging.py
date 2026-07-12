"""Attach a request ID and emit one completion log per request."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from constant.values import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id(request: Request | None = None) -> str | None:
    if request is not None:
        return getattr(request.state, "request_id", None)
    return request_id_context.get()


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid4())
        request.state.request_id = request_id
        token = request_id_context.set(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "request_complete method=%s path=%s status=%s duration_ms=%s request_id=%s",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
                request_id,
            )
            request_id_context.reset(token)
