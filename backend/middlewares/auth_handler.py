"""Authentication/authorization response helpers."""

from fastapi import Request
from fastapi.responses import JSONResponse

from domain.res import ErrorResponse
from exceptions.business_exception import AuthenticationError, AuthorizationError
from middlewares.request_logging import get_request_id


async def handle_auth_error(
    request: Request, exc: AuthenticationError | AuthorizationError
) -> JSONResponse:
    payload = ErrorResponse(
        code=exc.code,
        message=exc.message,
        request_id=get_request_id(request),
    )
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())
