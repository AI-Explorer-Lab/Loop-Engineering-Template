"""Serializable data models shared by the Codex orchestration layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, ClassVar, Mapping
from uuid import uuid4


PROJECT_TIMEZONE = timezone(timedelta(hours=8), "UTC+08:00")
SCHEMA_VERSION = 1
QUEUE_SCHEMA_VERSION = 2


def utc_now_iso() -> str:
    """Return a stable UTC+8 timestamp suitable for JSON."""

    return datetime.now(PROJECT_TIMEZONE).isoformat(timespec="milliseconds")


def generate_task_id() -> str:
    """Generate a UTC+8 task id that is also safe as a directory name."""

    timestamp = datetime.now(PROJECT_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{uuid4().hex[:8]}"


def generate_queue_id() -> str:
    """Generate a stable identifier for one ordered multi-task queue."""

    timestamp = datetime.now(PROJECT_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    return f"queue-{timestamp}-{uuid4().hex[:8]}"


class InfrastructureError(RuntimeError):
    """A local runtime failure that Codex cannot fix by changing business code."""


class RunStatus(str, Enum):
    """Lifecycle values persisted in ``state.json`` and ``result.json``."""

    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCESS = "success"
    MANUAL_REVIEW = "manual_review"
    INFRASTRUCTURE_ERROR = "infrastructure_error"

    @property
    def is_final(self) -> bool:
        return self in {
            RunStatus.PAUSED,
            RunStatus.CANCELLED,
            RunStatus.SUCCESS,
            RunStatus.MANUAL_REVIEW,
            RunStatus.INFRASTRUCTURE_ERROR,
        }


class ReviewStatus(str, Enum):
    """Human review state kept separate from machine execution status."""

    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class DeliveryStatus(str, Enum):
    """Post-review delivery and archive state, independent of run/review state."""

    NOT_READY = "not_ready"
    COMMIT_PENDING = "commit_pending"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ARCHIVE_PENDING = "archive_pending"
    ARCHIVED = "archived"
    FAILED = "failed"


class QueueStatus(str, Enum):
    """Lifecycle of one ordered, human-gated task queue."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    WAITING_REVIEW = "waiting_review"
    REJECTED = "rejected"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    COMPLETED = "completed"

    @property
    def is_final(self) -> bool:
        return self in {
            QueueStatus.CANCELLED,
            QueueStatus.REJECTED,
            QueueStatus.INFRASTRUCTURE_ERROR,
            QueueStatus.COMPLETED,
        }


class QueueTaskStatus(str, Enum):
    """Scheduling state of one child task inside a queue."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    REJECTED = "rejected"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class RunPhase(str, Enum):
    """Fine-grained checkpoints used to resume without repeating a prompt."""

    INITIALIZED = "initialized"
    PROMPT_PENDING = "prompt_pending"
    CODEX_TURN = "codex_turn"
    VALIDATION_PENDING = "validation_pending"
    VALIDATING = "validating"
    EVALUATION_PENDING = "evaluation_pending"
    EVALUATING = "evaluating"
    COMPLETED = "completed"


class PromptKind(str, Enum):
    INITIAL = "initial"
    REPAIR = "repair"
    REVIEW_REPAIR = "review_repair"
    EVALUATION_REPAIR = "evaluation_repair"


class JsonModel:
    """Small protocol-like base class for explicit JSON round trips."""

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - abstract contract
        raise NotImplementedError


@dataclass(slots=True)
class AuditEvent(JsonModel):
    """One append-only, ordered fact in ``events.jsonl``."""

    seq: int
    source: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    turn_number: int | None = None
    round_number: int | None = None
    redacted: bool = False
    timestamp: str = field(default_factory=utc_now_iso)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "source": self.source,
            "type": self.type,
            "turn_number": self.turn_number,
            "round_number": self.round_number,
            "payload": dict(self.payload),
            "redacted": self.redacted,
        }


@dataclass(slots=True)
class ReviewRecord(JsonModel):
    """Immutable local human decision bound to one exact diff."""

    task_id: str
    decision: ReviewStatus
    reviewer: str
    comment: str
    machine_status: RunStatus
    reviewed_diff_sha256: str
    commit_subject: str = ""
    review_number: int = 1
    reviewed_at: str = field(default_factory=utc_now_iso)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.decision is ReviewStatus.PENDING:
            raise ValueError("review decision cannot be pending")
        self.reviewer = str(self.reviewer).strip()
        self.comment = str(self.comment).strip()
        self.reviewed_diff_sha256 = str(self.reviewed_diff_sha256).strip()
        self.commit_subject = str(self.commit_subject).strip()
        if not self.reviewer:
            raise ValueError("reviewer must be a non-empty string")
        if not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_diff_sha256):
            raise ValueError("reviewed_diff_sha256 must be a lowercase SHA-256")
        if "\n" in self.commit_subject or "\r" in self.commit_subject:
            raise ValueError("commit_subject must be a single line")
        if len(self.commit_subject) > 200:
            raise ValueError("commit_subject must be at most 200 characters")
        self.review_number = int(self.review_number)
        if self.review_number < 1:
            raise ValueError("review_number must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "decision": self.decision.value,
            "reviewer": self.reviewer,
            "comment": self.comment,
            "machine_status": self.machine_status.value,
            "reviewed_diff_sha256": self.reviewed_diff_sha256,
            "commit_subject": self.commit_subject,
            "review_number": self.review_number,
            "reviewed_at": self.reviewed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewRecord":
        return cls(
            task_id=str(data["task_id"]),
            decision=ReviewStatus(str(data["decision"])),
            reviewer=str(data.get("reviewer", "")),
            comment=str(data.get("comment", "")),
            machine_status=RunStatus(str(data["machine_status"])),
            reviewed_diff_sha256=str(data.get("reviewed_diff_sha256", "")),
            commit_subject=str(data.get("commit_subject", "")),
            review_number=int(data.get("review_number", 1)),
            reviewed_at=str(data.get("reviewed_at") or utc_now_iso()),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(slots=True)
class TaskSpec(JsonModel):
    """One feature request handled by one Codex thread."""

    requirement: str
    acceptance_criteria: list[str]
    task_id: str = field(default_factory=generate_task_id)
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: int = SCHEMA_VERSION
    queue_id: str | None = None
    sequence: int | None = None
    rerun_of: str | None = None

    _TASK_ID_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$"
    )

    def __post_init__(self) -> None:
        self.requirement = str(self.requirement).strip()
        if not self.requirement:
            raise ValueError("requirement must be a non-empty string")

        if isinstance(self.acceptance_criteria, (str, bytes)):
            raise ValueError("acceptance_criteria must be a list of strings")
        self.acceptance_criteria = [
            str(criterion).strip() for criterion in self.acceptance_criteria
        ]
        if not self.acceptance_criteria or any(
            not criterion for criterion in self.acceptance_criteria
        ):
            raise ValueError(
                "acceptance_criteria must contain at least one non-empty string"
            )

        self.task_id = str(self.task_id or generate_task_id()).strip()
        if (
            "/" in self.task_id
            or "\\" in self.task_id
            or ".." in self.task_id
            or not self._TASK_ID_PATTERN.fullmatch(self.task_id)
        ):
            raise ValueError(
                "task_id must contain only letters, numbers, '.', '_' or '-' "
                "and must not contain path separators"
            )
        if self.queue_id is not None:
            self.queue_id = str(self.queue_id).strip()
            if (
                "/" in self.queue_id
                or "\\" in self.queue_id
                or ".." in self.queue_id
                or not self._TASK_ID_PATTERN.fullmatch(self.queue_id)
            ):
                raise ValueError("queue_id must be a safe identifier")
            if self.sequence is None or int(self.sequence) < 1:
                raise ValueError("queued tasks require a positive sequence")
            self.sequence = int(self.sequence)
        elif self.sequence is not None:
            raise ValueError("sequence requires queue_id")
        if self.rerun_of is not None:
            self.rerun_of = str(self.rerun_of).strip()
            if (
                "/" in self.rerun_of
                or "\\" in self.rerun_of
                or ".." in self.rerun_of
                or not self._TASK_ID_PATTERN.fullmatch(self.rerun_of)
            ):
                raise ValueError("rerun_of must be a safe task identifier")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "requirement": self.requirement,
            "acceptance_criteria": list(self.acceptance_criteria),
            "created_at": self.created_at,
        }
        if self.queue_id is not None:
            data["queue_id"] = self.queue_id
            data["sequence"] = self.sequence
        if self.rerun_of is not None:
            data["rerun_of"] = self.rerun_of
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec":
        criteria = data.get("acceptance_criteria")
        if not isinstance(criteria, list):
            raise ValueError("acceptance_criteria must be a JSON array of strings")
        task_id = data.get("task_id") or generate_task_id()
        return cls(
            task_id=str(task_id),
            requirement=str(data.get("requirement", "")),
            acceptance_criteria=list(criteria),
            created_at=str(data.get("created_at") or utc_now_iso()),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            queue_id=(
                None if data.get("queue_id") is None else str(data["queue_id"])
            ),
            sequence=(
                None if data.get("sequence") is None else int(data["sequence"])
            ),
            rerun_of=(
                None if data.get("rerun_of") is None else str(data["rerun_of"])
            ),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "TaskSpec":
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("task file must contain one JSON object")
        return cls.from_dict(data)


@dataclass(slots=True)
class TaskQueueSpec(JsonModel):
    """Immutable definition of one manually split, strictly ordered queue."""

    name: str
    subtasks: list[TaskSpec]
    queue_id: str = field(default_factory=generate_queue_id)
    base_ref: str = "HEAD"
    base_commit: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: int = QUEUE_SCHEMA_VERSION
    rerun_of: str | None = None

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("queue name must be a non-empty string")
        self.queue_id = str(self.queue_id).strip()
        if (
            "/" in self.queue_id
            or "\\" in self.queue_id
            or ".." in self.queue_id
            or not TaskSpec._TASK_ID_PATTERN.fullmatch(self.queue_id)
        ):
            raise ValueError("queue_id must be a safe identifier")
        self.base_ref = str(self.base_ref or "HEAD").strip()
        self.base_commit = str(self.base_commit).strip()
        if self.rerun_of is not None:
            self.rerun_of = str(self.rerun_of).strip()
            if (
                "/" in self.rerun_of
                or "\\" in self.rerun_of
                or ".." in self.rerun_of
                or not TaskSpec._TASK_ID_PATTERN.fullmatch(self.rerun_of)
            ):
                raise ValueError("rerun_of must be a safe queue identifier")
        if len(self.subtasks) < 2:
            raise ValueError("a task queue must contain at least two subtasks")
        ids: set[str] = set()
        for sequence, task in enumerate(self.subtasks, start=1):
            if task.task_id in ids:
                raise ValueError(f"duplicate subtask id: {task.task_id}")
            ids.add(task.task_id)
            if task.queue_id != self.queue_id:
                raise ValueError("every subtask must reference its queue_id")
            if task.sequence != sequence:
                raise ValueError("subtask sequence must be continuous and ordered")

    @classmethod
    def from_inputs(
        cls,
        name: str,
        subtasks: list[Mapping[str, Any]],
        *,
        queue_id: str | None = None,
        base_ref: str = "HEAD",
        rerun_of: str | None = None,
    ) -> "TaskQueueSpec":
        resolved_queue_id = str(queue_id or generate_queue_id())
        tasks: list[TaskSpec] = []
        for sequence, item in enumerate(subtasks, start=1):
            if not isinstance(item, Mapping):
                raise ValueError("every subtask must be a JSON object")
            criteria = item.get("acceptance_criteria", [])
            if not isinstance(criteria, list):
                raise ValueError(
                    "every subtask acceptance_criteria must be a JSON array"
                )
            tasks.append(
                TaskSpec(
                    task_id=f"{resolved_queue_id}-task-{sequence:02d}",
                    requirement=str(item.get("requirement", "")),
                    acceptance_criteria=list(criteria),
                    queue_id=resolved_queue_id,
                    sequence=sequence,
                    schema_version=QUEUE_SCHEMA_VERSION,
                )
            )
        return cls(
            queue_id=resolved_queue_id,
            name=name,
            subtasks=tasks,
            base_ref=base_ref,
            rerun_of=rerun_of,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "queue_id": self.queue_id,
            "name": self.name,
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "created_at": self.created_at,
            "subtasks": [task.to_dict() for task in self.subtasks],
        }
        if self.rerun_of is not None:
            data["rerun_of"] = self.rerun_of
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskQueueSpec":
        values = data.get("subtasks")
        if not isinstance(values, list):
            raise ValueError("subtasks must be a JSON array")
        if any(not isinstance(item, Mapping) for item in values):
            raise ValueError("every persisted subtask must be a JSON object")
        return cls(
            queue_id=str(data["queue_id"]),
            name=str(data.get("name", "")),
            base_ref=str(data.get("base_ref", "HEAD")),
            base_commit=str(data.get("base_commit", "")),
            created_at=str(data.get("created_at") or utc_now_iso()),
            schema_version=int(data.get("schema_version", QUEUE_SCHEMA_VERSION)),
            rerun_of=(
                None if data.get("rerun_of") is None else str(data["rerun_of"])
            ),
            subtasks=[TaskSpec.from_dict(item) for item in values],
        )


@dataclass(slots=True)
class QueueTaskState(JsonModel):
    """Durable scheduler projection for one queue subtask."""

    task_id: str
    sequence: int
    status: QueueTaskStatus = QueueTaskStatus.PENDING
    machine_status: RunStatus | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_READY
    thread_id: str | None = None
    last_error_summary: str = ""
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "sequence": self.sequence,
            "status": self.status.value,
            "machine_status": (
                None if self.machine_status is None else self.machine_status.value
            ),
            "review_status": self.review_status.value,
            "delivery_status": self.delivery_status.value,
            "thread_id": self.thread_id,
            "last_error_summary": self.last_error_summary,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QueueTaskState":
        machine_status = data.get("machine_status")
        return cls(
            task_id=str(data["task_id"]),
            sequence=int(data["sequence"]),
            status=QueueTaskStatus(str(data.get("status", "pending"))),
            machine_status=(
                None if machine_status is None else RunStatus(str(machine_status))
            ),
            review_status=ReviewStatus(str(data.get("review_status", "pending"))),
            delivery_status=DeliveryStatus(
                str(data.get("delivery_status", DeliveryStatus.NOT_READY.value))
            ),
            thread_id=(
                None if data.get("thread_id") is None else str(data["thread_id"])
            ),
            last_error_summary=str(data.get("last_error_summary", "")),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
        )


@dataclass(slots=True)
class QueueState(JsonModel):
    """Atomic checkpoint for a strictly serial task queue."""

    queue_id: str
    base_ref: str
    base_commit: str
    subtasks: list[QueueTaskState]
    status: QueueStatus = QueueStatus.PENDING
    current_task_id: str | None = None
    cumulative_diff_sha256: str = ""
    last_error_summary: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    schema_version: int = QUEUE_SCHEMA_VERSION

    @classmethod
    def from_spec(cls, spec: TaskQueueSpec) -> "QueueState":
        if not spec.base_commit:
            raise ValueError("queue spec must have a resolved base_commit")
        return cls(
            queue_id=spec.queue_id,
            base_ref=spec.base_ref,
            base_commit=spec.base_commit,
            subtasks=[
                QueueTaskState(task_id=task.task_id, sequence=int(task.sequence or 0))
                for task in spec.subtasks
            ],
        )

    def ordered_subtasks(self) -> list[QueueTaskState]:
        return sorted(self.subtasks, key=lambda item: item.sequence)

    def task(self, task_id: str) -> QueueTaskState:
        for task in self.subtasks:
            if task.task_id == task_id:
                return task
        raise ValueError(f"unknown queue subtask: {task_id}")

    def next_pending(self) -> QueueTaskState | None:
        for task in self.ordered_subtasks():
            if task.status in {
                QueueTaskStatus.COMPLETED,
                QueueTaskStatus.SKIPPED,
            }:
                continue
            if task.status is QueueTaskStatus.PENDING:
                return task
            return None
        return None

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "queue_id": self.queue_id,
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "status": self.status.value,
            "current_task_id": self.current_task_id,
            "cumulative_diff_sha256": self.cumulative_diff_sha256,
            "last_error_summary": self.last_error_summary,
            "subtasks": [task.to_dict() for task in self.ordered_subtasks()],
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QueueState":
        return cls(
            queue_id=str(data["queue_id"]),
            base_ref=str(data.get("base_ref", "HEAD")),
            base_commit=str(data.get("base_commit", "")),
            status=QueueStatus(str(data.get("status", "pending"))),
            current_task_id=(
                None
                if data.get("current_task_id") is None
                else str(data["current_task_id"])
            ),
            cumulative_diff_sha256=str(data.get("cumulative_diff_sha256", "")),
            last_error_summary=str(data.get("last_error_summary", "")),
            subtasks=[
                QueueTaskState.from_dict(item) for item in data.get("subtasks", [])
            ],
            started_at=str(data.get("started_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            finished_at=(
                None if data.get("finished_at") is None else str(data["finished_at"])
            ),
            schema_version=int(data.get("schema_version", QUEUE_SCHEMA_VERSION)),
        )


@dataclass(slots=True)
class CommandResult(JsonModel):
    """Complete, redacted-at-persistence result for one validation command."""

    command: list[str]
    cwd: str = ""
    stage: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    duration_seconds: float = 0.0
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    infrastructure_error: str | None = None
    log_path: str | None = None
    log_sha256: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.command, (str, bytes)) or not self.command:
            raise ValueError("command must be a non-empty list of arguments")
        self.command = [str(argument) for argument in self.command]
        self.cwd = str(self.cwd)
        if self.log_path is not None:
            self.log_path = str(self.log_path)
        if self.log_sha256 is not None:
            self.log_sha256 = str(self.log_sha256)
        self.duration_seconds = max(0.0, float(self.duration_seconds))
        if self.exit_code is not None:
            self.exit_code = int(self.exit_code)

    @property
    def passed(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.infrastructure_error
        )

    @property
    def failed(self) -> bool:
        return not self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "stage": self.stage,
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "infrastructure_error": self.infrastructure_error,
            "log_path": self.log_path,
            "log_sha256": self.log_sha256,
        }

    def metadata_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("stdout", None)
        data.pop("stderr", None)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CommandResult":
        command = data.get("command")
        if not isinstance(command, list):
            raise ValueError("command must be a JSON array")
        return cls(
            command=[str(argument) for argument in command],
            cwd=str(data.get("cwd", "")),
            stage=str(data.get("stage", "")),
            started_at=str(data.get("started_at") or utc_now_iso()),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            exit_code=(
                None if data.get("exit_code") is None else int(data["exit_code"])
            ),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            timed_out=bool(data.get("timed_out", False)),
            infrastructure_error=(
                None
                if data.get("infrastructure_error") is None
                else str(data["infrastructure_error"])
            ),
            log_path=(
                None if data.get("log_path") is None else str(data["log_path"])
            ),
            log_sha256=(
                None
                if data.get("log_sha256") is None
                else str(data["log_sha256"])
            ),
        )


@dataclass(slots=True)
class ValidationRound(JsonModel):
    """All targeted and full validation commands run after one Codex turn."""

    round_number: int
    targeted_results: list[CommandResult] = field(default_factory=list)
    full_results: list[CommandResult] = field(default_factory=list)
    passed: bool = False
    stage: str = "targeted"
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    failure_summary: str = ""
    infrastructure_error: str | None = None

    def __post_init__(self) -> None:
        self.round_number = int(self.round_number)
        if self.round_number < 1:
            raise ValueError("round_number must be at least 1")

    @property
    def command_results(self) -> list[CommandResult]:
        return [*self.targeted_results, *self.full_results]

    @property
    def failed_results(self) -> list[CommandResult]:
        return [result for result in self.command_results if result.failed]

    @property
    def log_paths(self) -> list[str]:
        return [
            result.log_path
            for result in self.command_results
            if result.log_path is not None
        ]

    def to_dict(self, *, include_output: bool = True) -> dict[str, Any]:
        serialize = (
            (lambda result: result.to_dict())
            if include_output
            else (lambda result: result.metadata_dict())
        )
        return {
            "round_number": self.round_number,
            "targeted_results": [serialize(result) for result in self.targeted_results],
            "full_results": [serialize(result) for result in self.full_results],
            "passed": self.passed,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_summary": self.failure_summary,
            "infrastructure_error": self.infrastructure_error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValidationRound":
        return cls(
            round_number=int(data["round_number"]),
            targeted_results=[
                CommandResult.from_dict(item)
                for item in data.get("targeted_results", [])
            ],
            full_results=[
                CommandResult.from_dict(item) for item in data.get("full_results", [])
            ],
            passed=bool(data.get("passed", False)),
            stage=str(data.get("stage", "targeted")),
            started_at=str(data.get("started_at") or utc_now_iso()),
            finished_at=(
                None if data.get("finished_at") is None else str(data["finished_at"])
            ),
            failure_summary=str(data.get("failure_summary", "")),
            infrastructure_error=(
                None
                if data.get("infrastructure_error") is None
                else str(data["infrastructure_error"])
            ),
        )


@dataclass(slots=True)
class RunState(JsonModel):
    """Durable workflow checkpoint for one task."""

    task_id: str
    repo_root: str
    schema_version: int = SCHEMA_VERSION
    queue_id: str | None = None
    sequence: int | None = None
    inherited_baseline: bool = False
    inherited_diff_sha256: str = ""
    control_repo_root: str = ""
    base_ref: str = "HEAD"
    base_commit: str = ""
    task_branch: str = ""
    worktree_relative_path: str = ""
    source_worktree_was_dirty: bool = False
    permission_verified: bool = False
    review_status: ReviewStatus = ReviewStatus.PENDING
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_READY
    last_diff_sha256: str = ""
    diff_redaction_count: int = 0
    status: RunStatus = RunStatus.RUNNING
    phase: RunPhase = RunPhase.INITIALIZED
    thread_id: str | None = None
    pending_prompt_kind: PromptKind | None = PromptKind.INITIAL
    failure_count: int = 0
    cycle_failure_count: int = 0
    turn_count: int = 0
    cycle_turn_count: int = 0
    rounds: list[ValidationRound] = field(default_factory=list)
    baseline_test_hashes: dict[str, str] = field(default_factory=dict)
    protected_test_paths: list[str] = field(default_factory=list)
    baseline_git_status: str = ""
    final_git_summary: str = ""
    last_error_summary: str = ""
    infrastructure_error: str | None = None
    pending_evaluation_summary: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None

    @property
    def project_root(self) -> str:
        """Compatibility alias for callers that use ``project_root``."""

        return self.repo_root

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def add_round(self, validation_round: ValidationRound) -> None:
        if any(
            existing.round_number == validation_round.round_number
            for existing in self.rounds
        ):
            raise ValueError(
                f"validation round {validation_round.round_number} already exists"
            )
        self.rounds.append(validation_round)
        self.last_error_summary = validation_round.failure_summary
        if validation_round.infrastructure_error:
            self.mark_infrastructure_error(validation_round.infrastructure_error)
        elif not validation_round.passed:
            self.failure_count += 1
            self.cycle_failure_count += 1
            self.phase = RunPhase.PROMPT_PENDING
            self.pending_prompt_kind = PromptKind.REPAIR
            self.touch()
        else:
            self.cycle_failure_count = 0
            self.touch()

    def reopen_for_review_changes(self) -> None:
        """Continue the same worktree/thread after a human requests changes."""

        self.status = RunStatus.RUNNING
        self.phase = RunPhase.PROMPT_PENDING
        self.pending_prompt_kind = PromptKind.REVIEW_REPAIR
        self.review_status = ReviewStatus.PENDING
        self.delivery_status = DeliveryStatus.NOT_READY
        self.cycle_failure_count = 0
        self.cycle_turn_count = 0
        self.infrastructure_error = None
        self.pending_evaluation_summary = ""
        self.finished_at = None
        self.touch()

    def reopen_after_infrastructure_error(
        self,
        *,
        incomplete_prompt_kind: PromptKind | None = None,
    ) -> None:
        """Retry the current checkpoint after the local environment is repaired."""

        if self.status is not RunStatus.INFRASTRUCTURE_ERROR:
            raise ValueError("only infrastructure_error runs can be retried")
        self.status = RunStatus.RUNNING
        self.infrastructure_error = None
        self.finished_at = None
        if incomplete_prompt_kind is not None:
            self.phase = RunPhase.PROMPT_PENDING
            self.pending_prompt_kind = incomplete_prompt_kind
        elif self.turn_count:
            self.phase = RunPhase.VALIDATION_PENDING
            self.pending_prompt_kind = None
        else:
            self.phase = RunPhase.PROMPT_PENDING
            self.pending_prompt_kind = PromptKind.INITIAL
        self.touch()

    def reopen_after_pause(self) -> None:
        """Resume the exact checkpoint retained by a cooperative pause."""

        if self.status is not RunStatus.PAUSED:
            raise ValueError("only paused runs can be resumed")
        self.status = RunStatus.RUNNING
        self.infrastructure_error = None
        self.finished_at = None
        self.touch()

    def mark_paused(self) -> None:
        """Release the executor while retaining the current workflow phase."""

        self.status = RunStatus.PAUSED
        self.finished_at = utc_now_iso()
        self.touch()

    def mark_cancelled(self) -> None:
        """Finish the run without treating a user cancellation as a failure."""

        self.status = RunStatus.CANCELLED
        self.phase = RunPhase.COMPLETED
        self.pending_prompt_kind = None
        self.finished_at = utc_now_iso()
        self.touch()

    def mark_success(self, final_git_summary: str = "") -> None:
        self.status = RunStatus.SUCCESS
        self.phase = RunPhase.COMPLETED
        self.pending_prompt_kind = None
        self.final_git_summary = final_git_summary
        self.finished_at = utc_now_iso()
        self.touch()

    def mark_manual_review(self, final_git_summary: str = "") -> None:
        self.status = RunStatus.MANUAL_REVIEW
        self.phase = RunPhase.COMPLETED
        self.pending_prompt_kind = None
        self.final_git_summary = final_git_summary
        self.finished_at = utc_now_iso()
        self.touch()

    def mark_infrastructure_error(
        self, message: str, final_git_summary: str = ""
    ) -> None:
        self.status = RunStatus.INFRASTRUCTURE_ERROR
        self.phase = RunPhase.COMPLETED
        self.pending_prompt_kind = None
        self.infrastructure_error = str(message)
        self.last_error_summary = str(message)
        self.final_git_summary = final_git_summary
        self.finished_at = utc_now_iso()
        self.touch()

    def to_dict(self, *, include_output: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "queue_id": self.queue_id,
            "sequence": self.sequence,
            "inherited_baseline": self.inherited_baseline,
            "inherited_diff_sha256": self.inherited_diff_sha256,
            "repo_root": self.repo_root,
            "control_repo_root": self.control_repo_root,
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "task_branch": self.task_branch,
            "worktree_relative_path": self.worktree_relative_path,
            "source_worktree_was_dirty": self.source_worktree_was_dirty,
            "permission_verified": self.permission_verified,
            "review_status": self.review_status.value,
            "delivery_status": self.delivery_status.value,
            "last_diff_sha256": self.last_diff_sha256,
            "diff_redaction_count": self.diff_redaction_count,
            "status": self.status.value,
            "phase": self.phase.value,
            "thread_id": self.thread_id,
            "pending_prompt_kind": (
                None
                if self.pending_prompt_kind is None
                else self.pending_prompt_kind.value
            ),
            "failure_count": self.failure_count,
            "cycle_failure_count": self.cycle_failure_count,
            "turn_count": self.turn_count,
            "cycle_turn_count": self.cycle_turn_count,
            "rounds": [
                validation_round.to_dict(include_output=include_output)
                for validation_round in self.rounds
            ],
            "baseline_test_hashes": dict(self.baseline_test_hashes),
            "protected_test_paths": sorted(set(self.protected_test_paths)),
            "baseline_git_status": self.baseline_git_status,
            "final_git_summary": self.final_git_summary,
            "last_error_summary": self.last_error_summary,
            "infrastructure_error": self.infrastructure_error,
            "pending_evaluation_summary": self.pending_evaluation_summary,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunState":
        prompt_kind = data.get("pending_prompt_kind")
        return cls(
            task_id=str(data["task_id"]),
            repo_root=str(data["repo_root"]),
            schema_version=int(data.get("schema_version", 0)),
            queue_id=(
                None if data.get("queue_id") is None else str(data["queue_id"])
            ),
            sequence=(
                None if data.get("sequence") is None else int(data["sequence"])
            ),
            inherited_baseline=bool(data.get("inherited_baseline", False)),
            inherited_diff_sha256=str(data.get("inherited_diff_sha256", "")),
            control_repo_root=str(data.get("control_repo_root", "")),
            base_ref=str(data.get("base_ref", "HEAD")),
            base_commit=str(data.get("base_commit", "")),
            task_branch=str(data.get("task_branch", "")),
            worktree_relative_path=str(data.get("worktree_relative_path", "")),
            source_worktree_was_dirty=bool(
                data.get("source_worktree_was_dirty", False)
            ),
            permission_verified=bool(data.get("permission_verified", False)),
            review_status=ReviewStatus(
                str(data.get("review_status", ReviewStatus.PENDING.value))
            ),
            delivery_status=DeliveryStatus(
                str(data.get("delivery_status", DeliveryStatus.NOT_READY.value))
            ),
            last_diff_sha256=str(data.get("last_diff_sha256", "")),
            diff_redaction_count=int(data.get("diff_redaction_count", 0)),
            status=RunStatus(str(data.get("status", RunStatus.RUNNING.value))),
            phase=RunPhase(str(data.get("phase", RunPhase.INITIALIZED.value))),
            thread_id=(
                None if data.get("thread_id") is None else str(data["thread_id"])
            ),
            pending_prompt_kind=(
                None if prompt_kind is None else PromptKind(str(prompt_kind))
            ),
            failure_count=int(data.get("failure_count", 0)),
            cycle_failure_count=int(
                data.get("cycle_failure_count", data.get("failure_count", 0))
            ),
            turn_count=int(data.get("turn_count", 0)),
            cycle_turn_count=int(
                data.get("cycle_turn_count", data.get("turn_count", 0))
            ),
            rounds=[
                ValidationRound.from_dict(item) for item in data.get("rounds", [])
            ],
            baseline_test_hashes={
                str(path): str(digest)
                for path, digest in dict(
                    data.get("baseline_test_hashes", {})
                ).items()
            },
            protected_test_paths=sorted(
                {str(path) for path in data.get("protected_test_paths", [])}
            ),
            baseline_git_status=str(data.get("baseline_git_status", "")),
            final_git_summary=str(data.get("final_git_summary", "")),
            last_error_summary=str(data.get("last_error_summary", "")),
            infrastructure_error=(
                None
                if data.get("infrastructure_error") is None
                else str(data["infrastructure_error"])
            ),
            pending_evaluation_summary=str(
                data.get("pending_evaluation_summary", "")
            ),
            started_at=str(data.get("started_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            finished_at=(
                None if data.get("finished_at") is None else str(data["finished_at"])
            ),
        )


@dataclass(slots=True)
class RunResult(JsonModel):
    """Self-contained machine-readable final result."""

    task_id: str
    status: RunStatus
    requirement: str
    acceptance_criteria: list[str]
    repo_root: str
    thread_id: str | None
    turn_count: int
    failure_count: int
    rounds: list[ValidationRound]
    baseline_git_status: str
    final_git_summary: str
    schema_version: int = SCHEMA_VERSION
    queue_id: str | None = None
    sequence: int | None = None
    review_status: ReviewStatus = ReviewStatus.PENDING
    delivery_status: DeliveryStatus = DeliveryStatus.NOT_READY
    workspace: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    final_diff_sha256: str = ""
    diff_redaction_count: int = 0
    log_paths: list[str] = field(default_factory=list)
    infrastructure_error: str | None = None
    started_at: str = ""
    finished_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.status.is_final:
            raise ValueError("RunResult status must be a final status")

    @classmethod
    def from_run(cls, task: TaskSpec, state: RunState) -> "RunResult":
        if not state.status.is_final:
            raise ValueError("cannot build a result before the run is final")
        log_paths = list(
            dict.fromkeys(
                path
                for validation_round in state.rounds
                for path in validation_round.log_paths
            )
        )
        return cls(
            task_id=task.task_id,
            status=state.status,
            requirement=task.requirement,
            acceptance_criteria=list(task.acceptance_criteria),
            repo_root=state.repo_root,
            thread_id=state.thread_id,
            turn_count=state.turn_count,
            failure_count=state.failure_count,
            rounds=list(state.rounds),
            baseline_git_status=state.baseline_git_status,
            final_git_summary=state.final_git_summary,
            schema_version=state.schema_version,
            queue_id=state.queue_id,
            sequence=state.sequence,
            review_status=state.review_status,
            delivery_status=state.delivery_status,
            workspace={
                "base_ref": state.base_ref,
                "base_commit": state.base_commit,
                "task_branch": state.task_branch,
                "worktree": state.worktree_relative_path,
                "source_worktree_was_dirty": state.source_worktree_was_dirty,
            },
            permissions={"verified": state.permission_verified},
            artifacts={
                "manifest": "manifest.json",
                "permissions": "permissions.json",
                "events": "events.jsonl",
                "files": "changes/files.json",
                "diff": "changes/final.diff",
                "report": "report.md",
            },
            final_diff_sha256=state.last_diff_sha256,
            diff_redaction_count=state.diff_redaction_count,
            log_paths=log_paths,
            infrastructure_error=state.infrastructure_error,
            started_at=state.started_at,
            finished_at=state.finished_at or utc_now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "queue_id": self.queue_id,
            "sequence": self.sequence,
            "status": self.status.value,
            "machine_status": self.status.value,
            "review_status": self.review_status.value,
            "delivery_status": self.delivery_status.value,
            "requirement": self.requirement,
            "acceptance_criteria": list(self.acceptance_criteria),
            "repo_root": self.repo_root,
            "thread_id": self.thread_id,
            "turn_count": self.turn_count,
            "retry_count": max(0, self.turn_count - 1),
            "failure_count": self.failure_count,
            "validation": {
                "passed": bool(
                    self.status is RunStatus.SUCCESS
                    and self.rounds
                    and self.rounds[-1].passed
                ),
                "rounds": len(self.rounds),
            },
            "rounds": [
                validation_round.to_dict(include_output=False)
                for validation_round in self.rounds
            ],
            "baseline_git_status": self.baseline_git_status,
            "final_git_summary": self.final_git_summary,
            "workspace": dict(self.workspace),
            "permissions": dict(self.permissions),
            "artifacts": dict(self.artifacts),
            "final_diff_sha256": self.final_diff_sha256,
            "diff_redaction_count": self.diff_redaction_count,
            "log_paths": list(self.log_paths),
            "infrastructure_error": self.infrastructure_error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunResult":
        return cls(
            task_id=str(data["task_id"]),
            status=RunStatus(str(data.get("machine_status", data.get("status")))),
            requirement=str(data.get("requirement", "")),
            acceptance_criteria=[
                str(item) for item in data.get("acceptance_criteria", [])
            ],
            repo_root=str(data.get("repo_root", "")),
            thread_id=(
                None if data.get("thread_id") is None else str(data["thread_id"])
            ),
            turn_count=int(data.get("turn_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            rounds=[
                ValidationRound.from_dict(item) for item in data.get("rounds", [])
            ],
            baseline_git_status=str(data.get("baseline_git_status", "")),
            final_git_summary=str(data.get("final_git_summary", "")),
            schema_version=int(data.get("schema_version", 0)),
            queue_id=(
                None if data.get("queue_id") is None else str(data["queue_id"])
            ),
            sequence=(
                None if data.get("sequence") is None else int(data["sequence"])
            ),
            review_status=ReviewStatus(
                str(data.get("review_status", ReviewStatus.PENDING.value))
            ),
            delivery_status=DeliveryStatus(
                str(data.get("delivery_status", DeliveryStatus.NOT_READY.value))
            ),
            workspace={
                str(key): value for key, value in dict(data.get("workspace", {})).items()
            },
            permissions={
                str(key): value
                for key, value in dict(data.get("permissions", {})).items()
            },
            artifacts={
                str(key): str(value)
                for key, value in dict(data.get("artifacts", {})).items()
            },
            final_diff_sha256=str(data.get("final_diff_sha256", "")),
            diff_redaction_count=int(data.get("diff_redaction_count", 0)),
            log_paths=[str(path) for path in data.get("log_paths", [])],
            infrastructure_error=(
                None
                if data.get("infrastructure_error") is None
                else str(data["infrastructure_error"])
            ),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at") or utc_now_iso()),
        )


__all__ = [
    "AuditEvent",
    "CommandResult",
    "DeliveryStatus",
    "InfrastructureError",
    "PromptKind",
    "QUEUE_SCHEMA_VERSION",
    "QueueState",
    "QueueStatus",
    "QueueTaskState",
    "QueueTaskStatus",
    "RunPhase",
    "RunResult",
    "RunState",
    "RunStatus",
    "ReviewRecord",
    "ReviewStatus",
    "SCHEMA_VERSION",
    "TaskSpec",
    "TaskQueueSpec",
    "ValidationRound",
    "generate_task_id",
    "generate_queue_id",
    "utc_now_iso",
]
