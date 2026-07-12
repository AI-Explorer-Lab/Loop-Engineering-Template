"""Errors that are safe to translate into stable API responses."""

from __future__ import annotations

from typing import Any


class BusinessException(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class AuthenticationError(BusinessException):
    def __init__(self, message: str = "Authentication is required") -> None:
        super().__init__("AUTHENTICATION_REQUIRED", message, status_code=401)


class AuthorizationError(BusinessException):
    def __init__(self, message: str = "This action is not allowed") -> None:
        super().__init__("ACTION_NOT_ALLOWED", message, status_code=403)
