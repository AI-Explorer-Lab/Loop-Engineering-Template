"""Per-project construction of the approved Harness control-plane components."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .archiver import ArchiveCoordinator
from .audit import AuditRecorder
from .context import ContextAssembler, ContextSnapshot
from .evaluation import EvaluationCoordinator
from .knowledge import KnowledgeGateway
from .mcp_client import LocalMcpClient
from .memory import MediumTermMemory
from .planner import PlannerService, append_plan_event
from .queue_workflow import QueueWorkflow
from .role_runner import StructuredRoleRunner
from .skills import SkillRegistry
from .state import StateStore
from .workflow import OrchestrationWorkflow
from .validation_profile import ValidationProfile


class HarnessRuntime:
    """Build fresh role/MCP clients while sharing only durable project state."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        project_id: str,
        knowledge_actor_id: str,
        knowledge_writer_actor_id: str,
        mcp_registry: str | Path,
        validation_timeout_seconds: float = 900.0,
        validation_profile: ValidationProfile | Mapping[str, object] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.project_id = str(project_id)
        self.knowledge_actor_id = str(knowledge_actor_id)
        self.knowledge_writer_actor_id = str(knowledge_writer_actor_id)
        self.mcp_registry = Path(mcp_registry).expanduser().resolve()
        self.validation_timeout_seconds = float(validation_timeout_seconds)
        self.validation_profile = (
            validation_profile
            if isinstance(validation_profile, ValidationProfile)
            else ValidationProfile.from_mapping(validation_profile)
        )
        self.registry = json.loads(self.mcp_registry.read_text(encoding="utf-8"))

    def context_assembler(self) -> ContextAssembler:
        client = LocalMcpClient(self.mcp_registry, mode="read")
        knowledge = KnowledgeGateway(
            project_id=self.project_id,
            registry_path=self.mcp_registry,
            client=client,
        )
        return ContextAssembler(
            knowledge,
            SkillRegistry(client),
            MediumTermMemory(self.repo_root, aliases=knowledge.aliases()),
        )

    def workflow(
        self,
        *,
        store: StateStore | None = None,
        base_ref: str = "HEAD",
        inherited_diff_path: str | Path | None = None,
        inherited_diff_sha256: str = "",
    ) -> OrchestrationWorkflow:
        roles = StructuredRoleRunner(self.repo_root)
        return OrchestrationWorkflow(
            self.repo_root,
            store=store,
            base_ref=base_ref,
            inherited_diff_path=inherited_diff_path,
            inherited_diff_sha256=inherited_diff_sha256,
            validation_timeout_seconds=self.validation_timeout_seconds,
            context_assembler=self.context_assembler(),
            evaluation_coordinator=EvaluationCoordinator(roles),
            knowledge_actor_id=self.knowledge_actor_id,
            validation_profile=self.validation_profile,
        )

    def queue_workflow(self) -> QueueWorkflow:
        return QueueWorkflow(
            self.repo_root,
            validation_timeout_seconds=self.validation_timeout_seconds,
            workflow_factory=lambda store, base, inherited, inherited_sha: self.workflow(
                store=store,
                base_ref=base,
                inherited_diff_path=inherited,
                inherited_diff_sha256=inherited_sha,
            ),
            archive_callback=self.archive,
            validation_profile=self.validation_profile,
        )

    def planner(self) -> PlannerService:
        return PlannerService(
            self.repo_root,
            StructuredRoleRunner(self.repo_root),
        )

    def assemble_planner_context(
        self,
        *,
        plan_id: str,
        query: str,
    ) -> ContextSnapshot:
        directory = (
            self.repo_root
            / ".codex-orchestrator"
            / "drafts"
            / plan_id
        )
        assembler = self.context_assembler()
        sink = lambda event_type, payload: append_plan_event(
            directory, event_type, payload
        )
        assembler.event_sink = sink
        assembler.knowledge.client.event_sink = sink
        assembler.skills.client.event_sink = sink
        return assembler.assemble(
            path=directory / "context.json",
            stage="planner",
            query=query,
            actor=self.knowledge_actor_id,
            include_memory=True,
        )

    def archive(self, task_id: str, *, store: StateStore | None = None) -> dict[str, Any]:
        run_store = store or StateStore(self.repo_root)
        task = run_store.load_task(task_id)
        state = run_store.load_state(task_id)
        run_dir = run_store.run_dir(task_id)
        audit = AuditRecorder(
            run_dir,
            state.repo_root,
            state.base_commit,
            inherited_baseline=state.inherited_baseline,
            queue_task=task.queue_id is not None,
        )
        assembler = self.context_assembler()
        sink = lambda event_type, payload: audit.append(event_type, payload)
        assembler.event_sink = sink
        assembler.knowledge.client.event_sink = sink
        assembler.skills.client.event_sink = sink
        context = assembler.assemble(
            path=run_dir / "archive" / "retrieval-context.json",
            stage="archive",
            query=" ".join([task.requirement, *task.acceptance_criteria]),
            actor=self.knowledge_actor_id,
            changed_paths=[
                str(item.get("path"))
                for item in self._optional_json(
                    run_dir / "changes" / "files.json"
                ).get("files", [])
            ],
        )
        coordinator = ArchiveCoordinator(
            self.repo_root,
            project_id=self.project_id,
            consumer_actor=self.knowledge_actor_id,
            writer_actor=self.knowledge_writer_actor_id,
            role_runner=StructuredRoleRunner(self.repo_root),
            memory=MediumTermMemory(
                self.repo_root,
                aliases=assembler.knowledge.aliases(),
            ),
            archive_client=LocalMcpClient(
                self.mcp_registry,
                mode="archive",
                event_sink=sink,
            ),
            registry_path=self.mcp_registry,
        )
        return coordinator.archive(task_id, store=run_store, archive_context=context)

    def retry_archive(
        self, task_id: str, *, store: StateStore | None = None
    ) -> dict[str, Any]:
        assembler = self.context_assembler()
        coordinator = ArchiveCoordinator(
            self.repo_root,
            project_id=self.project_id,
            consumer_actor=self.knowledge_actor_id,
            writer_actor=self.knowledge_writer_actor_id,
            role_runner=StructuredRoleRunner(self.repo_root),
            memory=MediumTermMemory(
                self.repo_root,
                aliases=assembler.knowledge.aliases(),
            ),
            archive_client=LocalMcpClient(self.mcp_registry, mode="archive"),
            registry_path=self.mcp_registry,
        )
        return coordinator.retry(task_id, store=store)

    def capabilities(self) -> dict[str, Any]:
        read_client = LocalMcpClient(self.mcp_registry, mode="read")
        archive_client = LocalMcpClient(self.mcp_registry, mode="archive")
        skills = read_client.call_tool("skill_list", {})
        roots = dict(self.registry.get("roots", {}))
        return {
            "status": "healthy",
            "knowledge_base_path": str(roots.get("knowledge", "")),
            "mcp_registry": str(self.mcp_registry),
            "mcp_read": read_client.health(),
            "mcp_archive": archive_client.health(),
            "skill_count": len(skills.get("skills", [])),
            "archive_backlog": self._archive_backlog(),
        }

    def _archive_backlog(self) -> int:
        root = self.repo_root / ".codex-orchestrator"
        paths = [*root.glob("runs/*/archive/outbox.json")]
        paths.extend(root.glob("queues/*/subtasks/*/archive/outbox.json"))
        count = 0
        for path in paths:
            if self._optional_json(path).get("status") != "completed":
                count += 1
        return count

    @staticmethod
    def _optional_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}


__all__ = ["HarnessRuntime"]
