"""HTTP-facing automatic Plan preview and human confirmation workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from codex_loop.harness_runtime import HarnessRuntime
from codex_loop.models import TaskQueueSpec, TaskSpec
from codex_loop.planner import PlanDraft
from codex_loop.state import QueueStore, StateStore

from ..exceptions.business_exception import TaskConflictError, TaskNotFoundError
from .queue_service import QueueService
from .task_service import TaskService


class PlanService:
    """Keep planning side-effect free until an explicit confirmation request."""

    def __init__(
        self,
        runtime: HarnessRuntime,
        task_service: TaskService,
        queue_service: QueueService,
    ) -> None:
        self.runtime = runtime
        self.task_service = task_service
        self.queue_service = queue_service
        self.repo_root = runtime.repo_root

    def create(
        self,
        *,
        name: str,
        requirement: str,
        acceptance_criteria: list[str],
    ) -> dict[str, Any]:
        plan_id = f"plan-{uuid4().hex[:16]}"
        context = self.runtime.assemble_planner_context(
            plan_id=plan_id,
            query=" ".join([requirement, *acceptance_criteria]),
        )
        draft = self.runtime.planner().generate(
            plan_id=plan_id,
            name=name,
            requirement=requirement,
            acceptance_criteria=acceptance_criteria,
            context=context,
        )
        return draft.model_dump(mode="json")

    def get(self, plan_id: str) -> dict[str, Any]:
        draft_path = (
            self.repo_root
            / ".codex-orchestrator"
            / "drafts"
            / plan_id
            / "draft.json"
        )
        if draft_path.is_file():
            return PlanDraft.model_validate_json(
                draft_path.read_text(encoding="utf-8")
            ).model_dump(mode="json")
        for path in (
            self.repo_root / ".codex-orchestrator" / "runs"
        ).glob("*/plan/draft.json"):
            value = self._json(path)
            if value.get("plan_id") == plan_id:
                return value
        for path in (
            self.repo_root / ".codex-orchestrator" / "queues"
        ).glob("*/plan/draft.json"):
            value = self._json(path)
            if value.get("plan_id") == plan_id:
                return value
        raise TaskNotFoundError(plan_id)

    def confirm(
        self,
        plan_id: str,
        *,
        reviewer: str,
        edited_draft: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_available()
        spec, plan_path = self.runtime.planner().confirm(
            plan_id,
            reviewer=reviewer,
            edited_draft=edited_draft,
        )
        confirmation = self._json(plan_path / "confirmation.json")
        if isinstance(spec, TaskSpec):
            snapshot = self.task_service.start_spec(spec)
            target_kind = "task"
        elif isinstance(spec, TaskQueueSpec):
            snapshot = self.queue_service.start_spec(spec)
            target_kind = "queue"
        else:  # pragma: no cover - PlannerService contract
            raise RuntimeError("unsupported confirmed plan target")
        return {
            "plan_id": plan_id,
            "target_kind": target_kind,
            "target": snapshot.to_dict(),
            "confirmation": confirmation,
        }

    def _ensure_available(self) -> None:
        active = self.task_service.executor.active_task_id()
        if active is not None:
            raise TaskConflictError(f"Another task is already running: {active}")
        unfinished_tasks = StateStore(self.repo_root).unfinished_task_ids()
        unfinished_queues = QueueStore(self.repo_root).unfinished_queue_ids()
        if unfinished_tasks or unfinished_queues:
            identifiers = [*unfinished_tasks, *unfinished_queues]
            raise TaskConflictError(
                "An unfinished task or queue must be completed first: "
                + ", ".join(identifiers)
            )

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path} must contain one object")
        return value


__all__ = ["PlanService"]
