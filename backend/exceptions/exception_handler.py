"""Register consistent and non-sensitive error responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from domain.res import ErrorResponse
from exceptions.business_exception import BusinessException
from middlewares.request_logging import get_request_id

logger = logging.getLogger(__name__)


async def handle_business_exception(request: Request, exc: BusinessException) -> JSONResponse:
    payload = ErrorResponse(
        code=exc.code,
        message=exc.message,
        request_id=get_request_id(request),
        details=exc.details,
    )
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


async def handle_unknown_exception(request: Request, exc: Exception) -> JSONResponse:
    request_id = get_request_id(request)
    logger.exception("Unhandled exception request_id=%s", request_id, exc_info=exc)
    payload = ErrorResponse(
        code="INTERNAL_ERROR",
        message="The request could not be completed",
        request_id=request_id,
    )
    return JSONResponse(status_code=500, content=payload.model_dump())


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(BusinessException, handle_business_exception)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unknown_exception)
