"""One-time project bootstrap for the shared backend architecture decision."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
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


@dataclass(slots=True)
class BackendArchitectureBootstrap:
    """Persist and reuse the first backend-architecture context exactly once."""

    repo_root: Path
    enabled: bool
    knowledge_id: str = BACKEND_ARCHITECTURE_KNOWLEDGE_ID

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
        event_sink: Any | None = None,
    ) -> ContextSnapshot | None:
        """Load the saved bootstrap context or read TK-DEC-001 once."""

        if not self.enabled:
            return None
        state = self.snapshot()
        status = str(state.get("status", BOOTSTRAP_PENDING))
        if status == BOOTSTRAP_COMPLETED:
            return None

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
        if event_sink is not None:
            event_sink(
                "backend_architecture.bootstrap_loaded",
                {
                    "knowledge_id": self.knowledge_id,
                    "snapshot_sha256": snapshot.snapshot_sha256,
                },
            )
        return snapshot

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
    "BOOTSTRAP_COMPLETED",
    "BOOTSTRAP_DISABLED",
    "BOOTSTRAP_FAILED",
    "BOOTSTRAP_IN_PROGRESS",
    "BOOTSTRAP_PENDING",
    "BOOTSTRAP_READY",
    "BackendArchitectureBootstrap",
]
