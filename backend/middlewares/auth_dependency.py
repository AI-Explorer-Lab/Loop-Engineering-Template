"""Resolve the current user explicitly through FastAPI dependency injection."""

from fastapi import Header

from config.config import settings
from domain.models import CurrentUser
from exceptions.business_exception import AuthenticationError


async def get_current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    x_user_name: str | None = Header(default=None, alias="X-User-Name"),
) -> CurrentUser:
    auth_enabled = bool(settings.get("auth.enabled", False))
    if auth_enabled and not x_user_id:
        raise AuthenticationError()
    return CurrentUser(
        user_id=x_user_id or "local-user",
        display_name=x_user_name or "Local User",
        roles=("developer",),
    )
