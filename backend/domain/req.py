"""Reusable request schemas."""

from pydantic import BaseModel, Field

from constant.values import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE


class PaginationRequest(BaseModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
