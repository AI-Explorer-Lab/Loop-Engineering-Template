"""Human-gated single-or-queue planning without acceptance-criteria invention."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import ContextSnapshot
from .models import (
    TaskQueueSpec,
    TaskSpec,
    generate_queue_id,
    generate_task_id,
    utc_now_iso,
)
from .role_runner import StructuredRoleRunner
from .state import _atomic_write_json, redact_sensitive_data


EventSink = Callable[[str, Mapping[str, Any]], None]


class PlannedSubtask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    requirement_slice: str = Field(min_length=1, max_length=20000)
    source_acceptance_ids: list[str] = Field(min_length=1, max_length=50)


class PlannerRoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "manual_input_required"] = "ready"
    execution_mode: Literal["single", "queue"]
    subtasks: list[PlannedSubtask] = Field(min_length=1, max_length=50)
    unassigned_acceptance_ids: list[str] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_mode(self) -> "PlannerRoleOutput":
        if self.execution_mode == "single" and len(self.subtasks) != 1:
            raise ValueError("single execution_mode requires exactly one subtask")
        if self.execution_mode == "queue" and len(self.subtasks) < 2:
            raise ValueError("queue execution_mode requires at least two subtasks")
        sequences = [item.sequence for item in self.subtasks]
        if sequences != list(range(1, len(self.subtasks) + 1)):
            raise ValueError("subtask sequences must be continuous and ordered")
        return self


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    plan_id: str
    name: str
    source_requirement_sha256: str
    context_sha256: str
    acceptance_criteria: dict[str, str]
    status: Literal["ready", "manual_input_required"] = "ready"
    execution_mode: Literal["single", "queue"]
    subtasks: list[PlannedSubtask]
    unassigned_acceptance_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    planner_thread_id: str
    created_at: str

    @model_validator(mode="after")
    def validate_mapping(self) -> "PlanDraft":
        PlannerRoleOutput.model_validate(
            {
                "status": self.status,
                "execution_mode": self.execution_mode,
                "subtasks": [item.model_dump() for item in self.subtasks],
                "unassigned_acceptance_ids": self.unassigned_acceptance_ids,
                "warnings": self.warnings,
            }
        )
        known = set(self.acceptance_criteria)
        referenced = {
            criterion
            for subtask in self.subtasks
            for criterion in subtask.source_acceptance_ids
        }
        unknown = referenced - known
        if unknown:
            raise ValueError(
                "planner referenced unknown acceptance ids: "
                + ", ".join(sorted(unknown))
            )
        missing = sorted(known - referenced)
        if sorted(set(self.unassigned_acceptance_ids)) != missing:
            raise ValueError("unassigned_acceptance_ids does not match the mapping")
        if missing and self.status != "manual_input_required":
            raise ValueError("unassigned criteria require manual_input_required")
        if not missing and self.status != "ready":
            raise ValueError("a complete plan must have ready status")
        return self


class PlannerService:
    """Generate a draft only; no task, queue, branch, or worktree is created."""

    def __init__(
        self,
        repo_root: str | Path,
        role_runner: StructuredRoleRunner,
        *,
        event_sink: EventSink | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.root = self.repo_root / ".codex-orchestrator"
        self.role_runner = role_runner
        self.event_sink = event_sink

    def generate(
        self,
        *,
        name: str,
        requirement: str,
        acceptance_criteria: list[str],
        context: ContextSnapshot,
        plan_id: str | None = None,
    ) -> PlanDraft:
        resolved_id = str(plan_id or f"plan-{uuid4().hex[:16]}")
        self._validate_id(resolved_id)
        criteria = self.acceptance_map(acceptance_criteria)
        source_sha = _source_sha(requirement, criteria)
        directory = self.root / "drafts" / resolved_id
        if directory.exists():
            unexpected = {
                path.name
                for path in directory.iterdir()
                if path.name not in {"context.json", "events.jsonl"}
            }
            if unexpected:
                raise ValueError(f"plan draft already exists: {resolved_id}")
        else:
            directory.mkdir(parents=True, exist_ok=False)
        input_value = {
            "schema_version": 1,
            "plan_id": resolved_id,
            "name": str(name).strip(),
            "requirement": str(requirement).strip(),
            "acceptance_criteria": criteria,
            "source_requirement_sha256": source_sha,
            "created_at": utc_now_iso(),
        }
        _atomic_write_json(directory / "input.json", input_value)
        _atomic_write_json(directory / "context.json", context.to_dict())
        prompt = json.dumps(
            {
                "task_name": input_value["name"],
                "requirement": input_value["requirement"],
                "acceptance_criteria": criteria,
                "allowed_actions": [
                    "choose single or queue",
                    "describe requirement slices",
                    "assign existing acceptance ids",
                    "order queue subtasks linearly",
                ],
                "forbidden_fields": [
                    "dependencies",
                    "new_acceptance_criteria",
                ],
                "context": context.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        role = self.role_runner.run(
            role="planner",
            prompt=prompt,
            output_model=PlannerRoleOutput,
            artifact_dir=directory / "role",
        )
        output = PlannerRoleOutput.model_validate(role.output.model_dump())
        referenced = {
            criterion
            for subtask in output.subtasks
            for criterion in subtask.source_acceptance_ids
        }
        missing = sorted(set(criteria) - referenced)
        status = "manual_input_required" if missing else "ready"
        warnings = list(output.warnings)
        if sorted(set(output.unassigned_acceptance_ids)) != missing:
            warnings.append("planner unassigned mapping was corrected deterministically")
        draft = PlanDraft(
            plan_id=resolved_id,
            name=input_value["name"],
            source_requirement_sha256=source_sha,
            context_sha256=context.snapshot_sha256,
            acceptance_criteria=criteria,
            status=status,
            execution_mode=output.execution_mode,
            subtasks=output.subtasks,
            unassigned_acceptance_ids=missing,
            warnings=list(dict.fromkeys(warnings)),
            planner_thread_id=role.thread_id,
            created_at=utc_now_iso(),
        )
        _atomic_write_json(directory / "draft.json", draft.model_dump(mode="json"))
        self._emit(
            "plan.generated",
            {
                "plan_id": resolved_id,
                "execution_mode": draft.execution_mode,
                "subtask_count": len(draft.subtasks),
                "status": draft.status,
                "context_sha256": draft.context_sha256,
            },
            artifact_root=directory,
        )
        return draft

    def load(self, plan_id: str) -> PlanDraft:
        self._validate_id(plan_id)
        path = self.root / "drafts" / plan_id / "draft.json"
        return PlanDraft.model_validate_json(path.read_text(encoding="utf-8"))

    def confirm(
        self,
        plan_id: str,
        *,
        reviewer: str,
        edited_draft: Mapping[str, Any] | None = None,
    ) -> tuple[TaskSpec | TaskQueueSpec, Path]:
        self._validate_id(plan_id)
        directory = self.root / "drafts" / plan_id
        original = PlanDraft.model_validate_json(
            (directory / "draft.json").read_text(encoding="utf-8")
        )
        candidate = (
            original
            if edited_draft is None
            else PlanDraft.model_validate(dict(edited_draft))
        )
        if candidate.plan_id != original.plan_id:
            raise ValueError("confirmed plan_id cannot change")
        if candidate.source_requirement_sha256 != original.source_requirement_sha256:
            raise ValueError("confirmed plan source hash cannot change")
        if candidate.context_sha256 != original.context_sha256:
            raise ValueError("confirmed plan context hash cannot change")
        if candidate.acceptance_criteria != original.acceptance_criteria:
            raise ValueError("confirmed plan acceptance criteria cannot change")
        if candidate.status != "ready":
            raise ValueError("manual_input_required plan cannot be confirmed")
        reviewer_value = str(reviewer).strip()
        if not reviewer_value:
            raise ValueError("plan reviewer must not be blank")
        if candidate.execution_mode == "single":
            subtask = candidate.subtasks[0]
            spec: TaskSpec | TaskQueueSpec = TaskSpec(
                task_id=generate_task_id(),
                requirement=subtask.requirement_slice,
                acceptance_criteria=[
                    candidate.acceptance_criteria[item]
                    for item in subtask.source_acceptance_ids
                ],
            )
            target_root = self.root / "runs" / spec.task_id
        else:
            queue_id = generate_queue_id()
            spec = TaskQueueSpec.from_inputs(
                candidate.name,
                [
                    {
                        "requirement": item.requirement_slice,
                        "acceptance_criteria": [
                            candidate.acceptance_criteria[criterion]
                            for criterion in item.source_acceptance_ids
                        ],
                    }
                    for item in candidate.subtasks
                ],
                queue_id=queue_id,
            )
            target_root = self.root / "queues" / spec.queue_id
        original_json = original.model_dump(mode="json")
        candidate_json = candidate.model_dump(mode="json")
        confirmation = {
            "schema_version": 1,
            "plan_id": plan_id,
            "reviewer": reviewer_value,
            "confirmed_at": utc_now_iso(),
            "original_plan_sha256": _json_sha(original_json),
            "confirmed_plan_sha256": _json_sha(candidate_json),
            "manual_edit_count": _difference_count(original_json, candidate_json),
            "target_kind": "task" if isinstance(spec, TaskSpec) else "queue",
            "target_id": spec.task_id if isinstance(spec, TaskSpec) else spec.queue_id,
            "confirmed_draft": candidate_json,
        }
        _atomic_write_json(directory / "confirmation.json", confirmation)
        target_root.mkdir(parents=True, exist_ok=False)
        target_plan = target_root / "plan"
        os.replace(directory, target_plan)
        self._emit(
            "plan.confirmed",
            {
                "plan_id": plan_id,
                "target_id": confirmation["target_id"],
                "target_kind": confirmation["target_kind"],
                "manual_edit_count": confirmation["manual_edit_count"],
            },
            artifact_root=target_plan,
        )
        return spec, target_plan

    @staticmethod
    def acceptance_map(values: list[str]) -> dict[str, str]:
        normalized = [str(item).strip() for item in values]
        if not normalized or any(not item for item in normalized):
            raise ValueError("acceptance_criteria must contain non-empty strings")
        return {f"AC-{index:03d}": value for index, value in enumerate(normalized, 1)}

    @staticmethod
    def _validate_id(plan_id: str) -> None:
        if not re.fullmatch(r"plan-[A-Za-z0-9][A-Za-z0-9._-]{2,126}", str(plan_id)):
            raise ValueError("plan_id is unsafe")

    def _emit(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        artifact_root: Path | None = None,
    ) -> None:
        if artifact_root is not None:
            append_plan_event(artifact_root, event_type, payload)
        if self.event_sink is not None:
            self.event_sink(event_type, dict(payload))


def append_plan_event(
    artifact_root: str | Path,
    event_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one redacted event to a draft or confirmed Plan timeline."""

    path = Path(artifact_root) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    sequence = 1
    if path.is_file():
        sequence = len(
            [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            ]
        ) + 1
    event = redact_sensitive_data(
        {
            "schema_version": 1,
            "seq": sequence,
            "timestamp": utc_now_iso(),
            "source": "orchestrator",
            "type": str(event_type),
            "payload": dict(payload),
        }
    )
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return event


def _source_sha(requirement: str, criteria: Mapping[str, str]) -> str:
    return _json_sha(
        {"requirement": str(requirement).strip(), "acceptance_criteria": dict(criteria)}
    )


def _json_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _difference_count(left: Any, right: Any) -> int:
    if type(left) is not type(right):
        return 1
    if isinstance(left, dict):
        keys = set(left) | set(right)
        return sum(_difference_count(left.get(key), right.get(key)) for key in keys)
    if isinstance(left, list):
        size = max(len(left), len(right))
        return sum(
            _difference_count(
                left[index] if index < len(left) else None,
                right[index] if index < len(right) else None,
            )
            for index in range(size)
        )
    return int(left != right)


__all__ = [
    "PlanDraft",
    "PlannedSubtask",
    "PlannerRoleOutput",
    "PlannerService",
    "append_plan_event",
]
