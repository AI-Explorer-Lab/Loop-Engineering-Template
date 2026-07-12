"""Stable enumerations shared by multiple business modules."""

from enum import StrEnum


class EnvironmentName(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LoopStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    RELEASING = "releasing"
    OBSERVING = "observing"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"
