import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from codex_loop.state import redact_sensitive_text

from ..constant.enums import ErrorCode
from .business_exception import BusinessException


logger = logging.getLogger(__name__)


def _error_body(request: Request, code: str, message: str) -> dict[str, object]:
    return {
        "success": False,
        "data": None,
        "message": redact_sensitive_text(message),
        "code": code,
        "request_id": getattr(request.state, "request_id", None),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BusinessException)
    async def handle_business_exception(
        request: Request, exc: BusinessException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(request, exc.code.value, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body(
                request,
                ErrorCode.BUSINESS_ERROR.value,
                "Invalid request payload",
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unknown_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled orchestrator API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_error_body(
                request,
                ErrorCode.INTERNAL_ERROR.value,
                "Internal server error",
            ),
        )
