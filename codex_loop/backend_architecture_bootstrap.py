"""One-time project bootstrap for the shared backend architecture decision."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .context import ContextAssembler, ContextSnapshot
from .models import InfrastructureError, utc_now_iso
from .state import _atomic_write_json


BACKEND_ARCHITECTURE_KNOWLEDGE_ID = "TK-DEC-001"
BOOTSTRAP_DISABLED = "disabled"
BOOTSTRAP_PENDING = "pending"
BOOTSTRAP_IN_PROGRESS = "in_progress"
BOOTSTRAP_READY = "ready"
BOOTSTRAP_COMPLETED = "completed"
BOOTSTRAP_FAILED = "failed"


BACKEND_SCAFFOLD_FILES: dict[str, str] = {
    "backend/__init__.py": '"""Project backend package initialized from TK-DEC-001."""\n',
    "backend/main.py": '''"""Application entrypoint scaffold; business wiring is added by the task."""\n\n\ndef create_app() -> object:\n    """Return the application instance once the project selects its web framework."""\n\n    raise NotImplementedError("backend application wiring is not implemented yet")\n''',
    "backend/config/__init__.py": '"""Configuration package."""\n',
    "backend/config/app.yaml": "# Non-sensitive application defaults.\nenvironment: development\n\n# Database design is intentionally not selected by the architecture bootstrap.\ndatabase:\n  enabled: false\n",
    "backend/config/config.py": '''"""Configuration loading boundary for the backend."""\n\n\ndef load_environment(name: str = "development") -> str:\n    return name\n\n\ndef validate_settings() -> None:\n    return None\n''',
    "backend/constant/__init__.py": '"""Shared constants and enums."""\n',
    "backend/constant/enums.py": '"""Stable cross-module enumerations."""\n',
    "backend/constant/values.py": '"""Stable cross-module values."""\n',
    "backend/domain/__init__.py": '"""Domain models independent of HTTP transport."""\n',
    "backend/domain/models.py": '"""Domain value objects and parameter objects."""\n',
    "backend/domain/req.py": '"""Request models belong here when API endpoints are added."""\n',
    "backend/domain/res.py": '"""Response models belong here when API endpoints are added."""\n',
    "backend/controller/__init__.py": '"""HTTP controller boundary."""\n',
    "backend/controller/health_api.py": '''"""Health-check endpoint boundary."""\n\n\ndef health() -> dict[str, str]:\n    return {"status": "ok"}\n''',
    "backend/service/__init__.py": '"""Application service boundary."""\n',
    "backend/middlewares/__init__.py": '"""Request middleware boundary."""\n',
    "backend/middlewares/request_logging.py": '"""Request ID, timing, and structured logging boundary."""\n',
    "backend/middlewares/auth_dependency.py": '"""Authentication dependency boundary."""\n',
    "backend/middlewares/auth_handler.py": '"""Authentication failure handling boundary."""\n',
    "backend/exceptions/__init__.py": '"""Business exception boundary."""\n',
    "backend/exceptions/business_exception.py": '"""Business exception types are defined here."""\n',
    "backend/exceptions/exception_handler.py": '"""Exception-to-response mapping boundary."""\n',
    "backend/mapper/__init__.py": '"""Persistence mapper boundary."""\n',
    "backend/utils/__init__.py": '"""Stateless reusable utilities."""\n',
    "backend/database/__init__.py": '"""Database lifecycle boundary."""\n',
    "backend/database/session.py": '"""Database session factory boundary; intentionally unconfigured."""\n',
    "backend/database/lifecycle.py": '"""Database lifecycle boundary; no tables are created by bootstrap."""\n',
    "backend/tests/__init__.py": '"""Backend test package initialized with the architecture scaffold."""\n',
    "backend/Dockerfile": "# Backend container boundary; runtime image is selected by a later task.\n",
    "backend/Jenkinsfile": "// CI pipeline boundary; stages are selected by a later task.\n",
    "backend/README.md": "# Backend architecture scaffold\n\nInitialized from TK-DEC-001. The backend skeleton is created independently of database design. Database files are retained as lifecycle boundaries, but no database, tables, migrations, or persistence configuration are created until a later task explicitly designs them.\n",
    "backend/.gitignore": "__pycache__/\n*.py[cod]\n.env\n",
    "backend/requirements.txt": "# Dependencies are added when the backend framework is selected.\n",
}


def backend_scaffold_files(project_name: str) -> dict[str, str]:
    """Return the fixed scaffold plus project-name-specific business files."""

    business_name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(project_name)).strip("-").lower()
    if not business_name:
        raise InfrastructureError("project name cannot produce a backend module name")
    return {
        **BACKEND_SCAFFOLD_FILES,
        f"backend/controller/{business_name}_api.py": (
            f'"""HTTP endpoints for the {business_name} business module."""\n'
        ),
        f"backend/service/{business_name}_service.py": (
            f'"""Use cases for the {business_name} business module."""\n'
        ),
    }


def materialize_backend_scaffold(
    worktree: str | Path,
    *,
    project_name: str,
    knowledge_id: str = BACKEND_ARCHITECTURE_KNOWLEDGE_ID,
    knowledge_content_sha256: str = "",
    snapshot_sha256: str = "",
) -> dict[str, Any]:
    """Create the source-project backend scaffold without overwriting files."""

    root = Path(worktree).expanduser().resolve()
    scaffold_files = backend_scaffold_files(project_name)
    created: list[str] = []
    for relative, content in scaffold_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            continue
        path.write_text(content, encoding="utf-8")
        created.append(relative)
    manifest = {
        "knowledge_id": knowledge_id,
        "knowledge_content_sha256": knowledge_content_sha256,
        "snapshot_sha256": snapshot_sha256,
        "database_design_enabled": False,
        "database_boundary_files": [
            "backend/database/session.py",
            "backend/database/lifecycle.py",
        ],
        "files": sorted(scaffold_files),
    }
    manifest_path = root / "backend" / ".architecture-bootstrap.json"
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        created.append("backend/.architecture-bootstrap.json")
    return {
        "knowledge_id": knowledge_id,
        "snapshot_sha256": snapshot_sha256,
        "created_paths": created,
    }


@dataclass(slots=True)
class BackendArchitectureBootstrap:
    """Persist and reuse the first backend-architecture context exactly once."""

    repo_root: Path
    enabled: bool
    knowledge_id: str = BACKEND_ARCHITECTURE_KNOWLEDGE_ID
    project_name: str = "business"

    @property
    def state_path(self) -> Path:
        return self.repo_root / ".codex-runtime" / "backend-architecture-bootstrap.json"

    @property
    def context_path(self) -> Path:
        return self.repo_root / ".codex-runtime" / "backend-architecture-context.json"

    def snapshot(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": BOOTSTRAP_DISABLED}
        value = self._load_state()
        value.setdefault("status", BOOTSTRAP_PENDING)
        value.setdefault("knowledge_id", self.knowledge_id)
        return value

    def prepare(
        self,
        *,
        task_id: str,
        assembler: ContextAssembler,
        actor: str,
        worktree: str | Path | None = None,
        event_sink: Any | None = None,
    ) -> ContextSnapshot | None:
        """Load the saved bootstrap context or read TK-DEC-001 once."""

        if not self.enabled:
            return None
        state = self.snapshot()
        status = str(state.get("status", BOOTSTRAP_PENDING))
        if status == BOOTSTRAP_COMPLETED:
            return None

        target_worktree = Path(worktree or self.repo_root).expanduser().resolve()

        existing_task = str(state.get("task_id", "")).strip()
        if existing_task and existing_task != task_id:
            raise InfrastructureError(
                "backend architecture bootstrap is still owned by "
                f"unfinished task {existing_task}"
            )

        if self.context_path.is_file():
            snapshot = ContextSnapshot.from_dict(
                json.loads(self.context_path.read_text(encoding="utf-8"))
            )
            snapshot.verify_hash()
            if not any(
                item.knowledge_id == self.knowledge_id
                for item in snapshot.knowledge
            ):
                raise InfrastructureError(
                    "saved backend architecture context does not contain "
                    f"{self.knowledge_id}"
                )
            materialized = self._materialize_scaffold(target_worktree, snapshot)
            self._save_state(
                {
                    **state,
                    "status": BOOTSTRAP_IN_PROGRESS,
                    "task_id": task_id,
                    "knowledge_id": self.knowledge_id,
                    "snapshot_sha256": snapshot.snapshot_sha256,
                }
            )
            if event_sink is not None:
                event_sink(
                    "backend_architecture.bootstrap_reused",
                    {
                        "knowledge_id": self.knowledge_id,
                        "snapshot_sha256": snapshot.snapshot_sha256,
                    },
                )
                event_sink(
                    "backend_architecture.scaffold_materialized",
                    materialized,
                )
            return snapshot

        self._save_state(
            {
                **state,
                "status": BOOTSTRAP_IN_PROGRESS,
                "task_id": task_id,
                "knowledge_id": self.knowledge_id,
                "started_at": state.get("started_at") or utc_now_iso(),
            }
        )
        try:
            snapshot = assembler.assemble_fixed(
                path=self.context_path,
                stage="generation",
                knowledge_id=self.knowledge_id,
                actor=actor,
            )
        except Exception as exc:
            self._save_state(
                {
                    **self.snapshot(),
                    "status": BOOTSTRAP_FAILED,
                    "task_id": "",
                    "error": str(exc),
                    "failed_at": utc_now_iso(),
                }
            )
            raise
        self._save_state(
            {
                **self.snapshot(),
                "status": BOOTSTRAP_IN_PROGRESS,
                "task_id": task_id,
                "knowledge_id": self.knowledge_id,
                "snapshot_sha256": snapshot.snapshot_sha256,
            }
        )
        materialized = self._materialize_scaffold(target_worktree, snapshot)
        if event_sink is not None:
            event_sink(
                "backend_architecture.bootstrap_loaded",
                {
                    "knowledge_id": self.knowledge_id,
                    "snapshot_sha256": snapshot.snapshot_sha256,
                },
            )
            event_sink(
                "backend_architecture.scaffold_materialized",
                materialized,
            )
        return snapshot

    def _materialize_scaffold(
        self, worktree: Path, snapshot: ContextSnapshot
    ) -> dict[str, Any]:
        """Materialize the fixed architecture template into the task worktree."""

        knowledge = next(
            (item for item in snapshot.knowledge if item.knowledge_id == self.knowledge_id),
            None,
        )
        if knowledge is None:
            raise InfrastructureError(
                f"backend architecture snapshot does not contain {self.knowledge_id}"
            )
        materialized = materialize_backend_scaffold(
            worktree,
            project_name=self.project_name,
            knowledge_id=self.knowledge_id,
            knowledge_content_sha256=knowledge.content_sha256,
            snapshot_sha256=snapshot.snapshot_sha256,
        )
        manifest = {
            "knowledge_id": self.knowledge_id,
            "knowledge_content_sha256": knowledge.content_sha256,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "database_design_enabled": False,
            "database_boundary_files": [
                "backend/database/session.py",
                "backend/database/lifecycle.py",
            ],
            "files": sorted(backend_scaffold_files(self.project_name)),
        }
        return {
            **materialized,
            "scaffold_sha256": hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }

    def mark_delivered(self, task_id: str) -> None:
        """Mark the bootstrap complete only after review-bound delivery succeeds."""

        if not self.enabled:
            return
        state = self.snapshot()
        if str(state.get("task_id", "")).strip() != task_id:
            return
        self._save_state(
            {
                **state,
                "status": BOOTSTRAP_COMPLETED,
                "task_id": "",
                "completed_at": utc_now_iso(),
                "error": "",
            }
        )

    def mark_failed(self, task_id: str, *, reason: str = "") -> None:
        if not self.enabled:
            return
        state = self.snapshot()
        if str(state.get("task_id", "")).strip() != task_id:
            return
        self._save_state(
            {
                **state,
                "status": BOOTSTRAP_FAILED,
                "task_id": "",
                "error": reason,
                "failed_at": utc_now_iso(),
            }
        )

    def initial_prompt_block(self, task_id: str) -> str:
        if not self.enabled:
            return ""
        state = self.snapshot()
        if (
            state.get("status") != BOOTSTRAP_IN_PROGRESS
            or str(state.get("task_id", "")).strip() != task_id
        ):
            return ""
        return (
            "\n# 首次后端架构初始化\n"
            f"- 本项目首次后端开发必须依据已冻结的 MCP 知识 {self.knowledge_id}。\n"
            "- 先搭建并验证后端的基础目录、模块边界、配置、数据访问和 API 骨架，"
            "再实现本次日记业务需求。\n"
            "- 不要重新调用 MCP；架构知识已经由控制面冻结在当前任务上下文中。\n"
            "- 如果需求与架构模板冲突，保留清晰的项目内实现边界，并在最终回复中说明偏离。\n"
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {
                "status": BOOTSTRAP_PENDING,
                "knowledge_id": self.knowledge_id,
            }
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InfrastructureError(
                "backend architecture bootstrap state cannot be read"
            ) from exc
        if not isinstance(value, dict):
            raise InfrastructureError(
                "backend architecture bootstrap state must be an object"
            )
        return dict(value)

    def _save_state(self, value: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.state_path, value)


__all__ = [
    "BACKEND_ARCHITECTURE_KNOWLEDGE_ID",
    "backend_scaffold_files",
    "materialize_backend_scaffold",
    "BOOTSTRAP_COMPLETED",
    "BOOTSTRAP_DISABLED",
    "BOOTSTRAP_FAILED",
    "BOOTSTRAP_IN_PROGRESS",
    "BOOTSTRAP_PENDING",
    "BOOTSTRAP_READY",
    "BackendArchitectureBootstrap",
]
