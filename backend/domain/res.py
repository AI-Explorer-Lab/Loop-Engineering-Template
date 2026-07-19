from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .models import QueueSnapshot, TaskSnapshot


T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T | None = None
    message: str = "ok"
    request_id: str | None = None


class HealthData(BaseModel):
    status: str
    environment: str
    version: str


class ProjectData(BaseModel):
    project_id: str
    name: str
    repo_root: str
    is_default: bool
    active_identifier: str | None = None
    knowledge_actor_id: str = ""


class HistoryItemData(BaseModel):
    kind: str
    identifier: str
    project_id: str
    project_name: str
    title: str
    status: str
    review_status: str | None = None
    started_at: str
    updated_at: str
    finished_at: str | None = None
    current_task_id: str | None = None
    delivery_status: str | None = None


class HistoryPageData(BaseModel):
    items: list[HistoryItemData]
    page: int
    page_size: int
    total: int
    pages: int


class EventPageData(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: int
    terminal: bool


class LogData(BaseModel):
    log_id: str
    name: str
    size: int
    sha256: str


class NotificationData(BaseModel):
    notification_id: str
    project_id: str
    kind: str
    identifier: str
    category: str
    title: str
    message: str
    created_at: str
    read_at: str | None = None
    delivery: dict[str, str] = Field(default_factory=dict)


class NotificationSettingsData(BaseModel):
    in_app: bool
    browser: bool
    email_configured: bool
    webhook_configured: bool


class TaskData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    requirement: str
    acceptance_criteria: list[str]
    status: str
    schema_version: int = 1
    legacy: bool = False
    history_warning: str | None = None
    machine_status: str | None = None
    review_status: str = "pending"
    delivery_status: str = "not_ready"
    phase: str | None = None
    thread_id: str | None = None
    turn_count: int = 0
    failure_count: int = 0
    cycle_turn_count: int = 0
    cycle_failure_count: int = 0
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    last_error_summary: str = ""
    infrastructure_error: str | None = None
    started_at: str = ""
    updated_at: str = ""
    finished_at: str | None = None
    report_url: str | None = None
    diff_url: str | None = None
    workspace: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    audit_summary: dict[str, Any] = Field(default_factory=dict)
    changed_files: list[dict[str, Any]] = Field(default_factory=list)
    codex_responses: list[dict[str, Any]] = Field(default_factory=list)
    final_diff_sha256: str = ""
    diff_redaction_count: int = 0
    review: dict[str, Any] | None = None
    review_history: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    evaluations: dict[str, Any] = Field(default_factory=dict)
    commit: dict[str, Any] = Field(default_factory=dict)
    archive: dict[str, Any] = Field(default_factory=dict)
    queue_id: str | None = None
    sequence: int | None = None
    rerun_of: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: TaskSnapshot) -> "TaskData":
        return cls.model_validate(snapshot.to_dict())


class QueueSubtaskData(BaseModel):
    task_id: str
    sequence: int
    requirement: str
    acceptance_criteria: list[str]
    status: str
    machine_status: str | None = None
    review_status: str
    delivery_status: str = "not_ready"
    thread_id: str | None = None
    last_error_summary: str = ""
    updated_at: str


class QueueData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_id: str
    name: str
    status: str
    base_ref: str
    base_commit: str
    current_task_id: str | None = None
    cumulative_diff_sha256: str = ""
    last_error_summary: str = ""
    delivery_status: str = "not_ready"
    subtasks: list[QueueSubtaskData]
    started_at: str
    updated_at: str
    finished_at: str | None = None
    report_url: str | None = None
    diff_url: str | None = None
    rerun_of: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: QueueSnapshot) -> "QueueData":
        return cls.model_validate(snapshot.to_dict())
