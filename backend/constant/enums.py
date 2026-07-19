from enum import StrEnum


class ApiTaskStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCESS = "success"
    MANUAL_REVIEW = "manual_review"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class ErrorCode(StrEnum):
    BUSINESS_ERROR = "BUSINESS_ERROR"
    INVALID_TASK_ID = "INVALID_TASK_ID"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_CONFLICT = "TASK_CONFLICT"
    TASK_NOT_READY = "TASK_NOT_READY"
    INVALID_QUEUE_ID = "INVALID_QUEUE_ID"
    QUEUE_NOT_FOUND = "QUEUE_NOT_FOUND"
    QUEUE_NOT_READY = "QUEUE_NOT_READY"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
