"""Framework-independent domain values."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user_id: str
    display_name: str | None = None
    roles: tuple[str, ...] = ()
