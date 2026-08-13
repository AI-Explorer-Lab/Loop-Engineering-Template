from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from threading import BoundedSemaphore
from typing import Any

from codex_loop.harness_runtime import HarnessRuntime
from codex_loop.backend_architecture_bootstrap import (
    BACKEND_ARCHITECTURE_KNOWLEDGE_ID,
    BackendArchitectureBootstrap,
)
from codex_loop.git_publish import GitPublishService
from codex_loop.validation_profile import ValidationProfile

from ..config.config import knowledge_from_settings, projects_from_settings
from ..config.project_store import (
    append_created_project,
    default_project_validation,
)
from ..exceptions.business_exception import ProjectNotFoundError
from ..utils.task_executor import TaskExecutor
from .queue_service import QueueService
from .task_service import TaskService
from .plan_service import PlanService
from .project_provisioning import provision_git_project


@dataclass(frozen=True, slots=True)
class ProjectContext:
    project_id: str
    name: str
    repo_root: Path
    is_default: bool
    knowledge_actor_id: str
    publish_enabled: bool
    publish_auto_create_remote: bool
    publish_remote_name: str
    publish_remote_url: str
    publish_repository_name: str
    publish_branch: str
    backend_architecture_enabled: bool
    backend_architecture_knowledge_id: str
    backend_architecture_bootstrap: BackendArchitectureBootstrap
    harness: HarnessRuntime
    task_service: TaskService
    queue_service: QueueService
    plan_service: PlanService


class ProjectRegistry:
    """Own one isolated executor pair per allowlisted repository."""

    def __init__(self, config: Any) -> None:
        self._config = config
        agent = config.get("agent", {}) or {}
        timeout = float(agent.get("validation_timeout_seconds", 900))
        knowledge = knowledge_from_settings(config)
        self._timeout = timeout
        self._knowledge = knowledge
        self._gate = BoundedSemaphore(int(agent.get("max_parallel_projects", 1)))
        self._contexts: dict[str, ProjectContext] = {}
        self._default_id = ""
        for item in projects_from_settings(config):
            context = self._build_context(item)
            self._contexts[context.project_id] = context
            if context.is_default:
                self._default_id = context.project_id

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

    def create_project(
        self,
        *,
        name: str,
        repo_path: str,
        backend_architecture_enabled: bool = False,
    ) -> ProjectContext:
        """Provision, register, and make available one new project immediately."""

        normalized_name = str(name).strip()
        if not normalized_name:
            raise ValueError("project name cannot be blank")
        path = Path(str(repo_path)).expanduser()
        if not path.is_absolute():
            raise ValueError("project path must be absolute")
        path = path.resolve()
        project_id = self._new_project_id(normalized_name, path)
        provision_git_project(
            path,
            project_id=project_id,
            project_name=normalized_name,
        )
        item = {
            "id": project_id,
            "project_id": project_id,
            "name": normalized_name,
            "repo_root": str(path),
            "default": False,
            "is_default": False,
            "knowledge_actor_id": "local-user",
            "backend_architecture_enabled": bool(backend_architecture_enabled),
            "backend_architecture_knowledge_id": BACKEND_ARCHITECTURE_KNOWLEDGE_ID,
            "publish": {
                "auto_create_remote": True,
                "remote_name": "origin",
                "visibility": "private",
                "branch": "main",
            },
            "validation": default_project_validation(),
        }
        item["validation_profile"] = ValidationProfile.from_mapping(item["validation"])
        context: ProjectContext | None = None
        try:
            context = self._build_context(item)
            append_created_project(
                self._config,
                {key: value for key, value in item.items() if key != "validation_profile"},
            )
        except Exception:
            if context is not None:
                context.task_service.close(wait=True)
            shutil.rmtree(path, ignore_errors=True)
            raise
        assert context is not None
        self._contexts[project_id] = context
        return context

    def close(self, *, wait: bool = False) -> None:
        for context in self._contexts.values():
            context.task_service.close(wait=wait)

    def _build_context(self, item: dict[str, object]) -> ProjectContext:
        project_id = str(item["project_id"])
        executor = TaskExecutor(global_gate=self._gate)
        root = Path(item["repo_root"])
        publish = item.get("publish", {})
        publish_config = publish if isinstance(publish, dict) else {}
        publish_enabled = bool(publish_config.get("enabled", False))
        publish_auto_create_remote = bool(
            publish_config.get("auto_create_remote", item.get("_created_from_registry", False))
        )
        publish_remote_name = str(
            publish_config.get("remote_name", "origin")
        ).strip() or "origin"
        publish_remote_url = str(publish_config.get("remote_url", "")).strip()
        publish_repository_name = root.name
        publish_branch = str(
            publish_config.get("branch", "main" if publish_auto_create_remote else "")
        ).strip()
        validation_profile = item["validation_profile"]
        backend_architecture_enabled = bool(
            item.get("backend_architecture_enabled", False)
        )
        backend_architecture_knowledge_id = str(
            item.get(
                "backend_architecture_knowledge_id",
                BACKEND_ARCHITECTURE_KNOWLEDGE_ID,
            )
        ).strip() or BACKEND_ARCHITECTURE_KNOWLEDGE_ID
        backend_architecture_bootstrap = BackendArchitectureBootstrap(
            root,
            enabled=backend_architecture_enabled,
            knowledge_id=backend_architecture_knowledge_id,
        )
        harness = HarnessRuntime(
            root,
            project_id=project_id,
            knowledge_actor_id=str(item.get("knowledge_actor_id", "")),
            knowledge_writer_actor_id=str(
                self._knowledge.get("knowledge_writer_actor_id", "")
            ),
            mcp_registry=str(self._knowledge.get("mcp_registry", "")),
            validation_timeout_seconds=self._timeout,
            validation_profile=validation_profile,
            backend_architecture_bootstrap=backend_architecture_bootstrap,
        )
        workflow_factory = harness.workflow
        queue_workflow_factory = harness.queue_workflow
        tasks = TaskService(
            root,
            validation_timeout_seconds=self._timeout,
            executor=executor,
            workflow_factory=workflow_factory,
            queue_workflow_factory=queue_workflow_factory,
            archive_callback=harness.archive,
            archive_retry_callback=harness.retry_archive,
            publish_service=self._publish_service(root, item),
            bootstrap_complete_callback=backend_architecture_bootstrap.mark_delivered,
            bootstrap_failed_callback=backend_architecture_bootstrap.mark_failed,
        )
        queues = QueueService(
            root,
            validation_timeout_seconds=self._timeout,
            executor=executor,
            workflow_factory=queue_workflow_factory,
        )
        plans = PlanService(harness, tasks, queues)
        return ProjectContext(
            project_id=project_id,
            name=str(item["name"]),
            repo_root=root,
            is_default=bool(item["is_default"]),
            knowledge_actor_id=str(item.get("knowledge_actor_id", "")),
            publish_enabled=publish_enabled,
            publish_auto_create_remote=publish_auto_create_remote,
            publish_remote_name=publish_remote_name,
            publish_remote_url=publish_remote_url,
            publish_repository_name=publish_repository_name,
            publish_branch=publish_branch,
            backend_architecture_enabled=backend_architecture_enabled,
            backend_architecture_knowledge_id=backend_architecture_knowledge_id,
            backend_architecture_bootstrap=backend_architecture_bootstrap,
            harness=harness,
            task_service=tasks,
            queue_service=queues,
            plan_service=plans,
        )

    def _new_project_id(self, name: str, path: Path) -> str:
        candidate = re.sub(r"[^A-Za-z0-9_-]+", "-", path.name or name).strip("-_")
        if not re.match(r"^[A-Za-z0-9]", candidate):
            candidate = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-_")
        candidate = candidate[:64] or "project"
        if not re.match(r"^[A-Za-z0-9]", candidate):
            candidate = "project"
        existing = set(self._contexts)
        if candidate not in existing:
            return candidate
        index = 2
        while f"{candidate[:61]}-{index}" in existing:
            index += 1
        return f"{candidate[:61]}-{index}"

    @staticmethod
    def _publish_service(root: Path, item: dict[str, object]) -> GitPublishService | None:
        publish = item.get("publish", {})
        publish_config = publish if isinstance(publish, dict) else {}
        auto_create_remote = bool(
            publish_config.get("auto_create_remote", item.get("_created_from_registry", False))
        )
        publish_enabled = bool(publish_config.get("enabled", False))
        if (
            publish_enabled
            and not str(publish_config.get("remote_url", "")).strip()
            and not auto_create_remote
        ):
            raise RuntimeError("publish.remote_url is required when publication is enabled")
        if not publish_enabled and not auto_create_remote:
            return None
        remote_name = str(publish_config.get("remote_name", "origin")).strip() or "origin"
        remote_url = str(publish_config.get("remote_url", "")).strip()
        publish_branch = str(
            publish_config.get("branch", "main" if auto_create_remote else "")
        ).strip()
        return GitPublishService(
            root,
            remote_name=remote_name,
            remote_url=remote_url,
            auto_create_remote=auto_create_remote,
            repository_name=str(
                publish_config.get("repository_name", root.name)
            ).strip(),
            visibility=str(publish_config.get("visibility", "private")),
            publish_branch=publish_branch,
        )
