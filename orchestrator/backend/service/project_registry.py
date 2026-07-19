from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any

from orchestrator.codex_loop.harness_runtime import HarnessRuntime
from orchestrator.codex_loop.queue_workflow import QueueWorkflow
from orchestrator.codex_loop.workflow import OrchestrationWorkflow

from ..config.config import knowledge_from_settings, projects_from_settings
from ..exceptions.business_exception import ProjectNotFoundError
from ..utils.task_executor import TaskExecutor
from .queue_service import QueueService
from .task_service import TaskService
from .plan_service import PlanService


@dataclass(frozen=True, slots=True)
class ProjectContext:
    project_id: str
    name: str
    repo_root: Path
    is_default: bool
    knowledge_actor_id: str
    harness: HarnessRuntime | None
    task_service: TaskService
    queue_service: QueueService
    plan_service: PlanService | None


class ProjectRegistry:
    """Own one isolated executor pair per allowlisted repository."""

    def __init__(self, config: Any) -> None:
        agent = config.get("agent", {}) or {}
        timeout = float(agent.get("validation_timeout_seconds", 900))
        harness_enabled = bool(agent.get("harness_enabled", False))
        knowledge = knowledge_from_settings(config)
        gate = BoundedSemaphore(int(agent.get("max_parallel_projects", 1)))
        self._contexts: dict[str, ProjectContext] = {}
        self._default_id = ""
        for item in projects_from_settings(config):
            project_id = str(item["project_id"])
            executor = TaskExecutor(global_gate=gate)
            root = Path(item["repo_root"])
            validation_profile = item["validation_profile"]
            harness = (
                HarnessRuntime(
                    root,
                    project_id=project_id,
                    knowledge_actor_id=str(item.get("knowledge_actor_id", "")),
                    knowledge_writer_actor_id=str(
                        knowledge.get("knowledge_writer_actor_id", "")
                    ),
                    mcp_registry=str(knowledge.get("mcp_registry", "")),
                    validation_timeout_seconds=timeout,
                    validation_profile=validation_profile,
                )
                if harness_enabled
                else None
            )
            workflow_factory = (
                harness.workflow
                if harness is not None
                else lambda root=root, profile=validation_profile: OrchestrationWorkflow(
                    root,
                    validation_timeout_seconds=timeout,
                    validation_profile=profile,
                )
            )
            queue_workflow_factory = (
                harness.queue_workflow
                if harness is not None
                else lambda root=root, profile=validation_profile: QueueWorkflow(
                    root,
                    validation_timeout_seconds=timeout,
                    validation_profile=profile,
                )
            )
            tasks = TaskService(
                root,
                validation_timeout_seconds=timeout,
                executor=executor,
                workflow_factory=workflow_factory,
                queue_workflow_factory=queue_workflow_factory,
                archive_callback=(None if harness is None else harness.archive),
                archive_retry_callback=(
                    None if harness is None else harness.retry_archive
                ),
            )
            queues = QueueService(
                root,
                validation_timeout_seconds=timeout,
                executor=executor,
                workflow_factory=queue_workflow_factory,
            )
            plans = (
                None if harness is None else PlanService(harness, tasks, queues)
            )
            context = ProjectContext(
                project_id=project_id,
                name=str(item["name"]),
                repo_root=root,
                is_default=bool(item["is_default"]),
                knowledge_actor_id=str(item.get("knowledge_actor_id", "")),
                harness=harness,
                task_service=tasks,
                queue_service=queues,
                plan_service=plans,
            )
            self._contexts[project_id] = context
            if context.is_default:
                self._default_id = project_id

    @property
    def default(self) -> ProjectContext:
        return self._contexts[self._default_id]

    def get(self, project_id: str | None = None) -> ProjectContext:
        resolved = str(project_id or self._default_id).strip()
        try:
            return self._contexts[resolved]
        except KeyError:
            raise ProjectNotFoundError(resolved) from None

    def all(self) -> list[ProjectContext]:
        return sorted(
            self._contexts.values(),
            key=lambda item: (not item.is_default, item.name.casefold()),
        )

    def close(self, *, wait: bool = False) -> None:
        for context in self._contexts.values():
            context.task_service.close(wait=wait)
