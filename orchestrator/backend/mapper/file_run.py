from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.codex_loop.models import CommandResult, TaskSpec, ValidationRound
from orchestrator.codex_loop.state import StateStore

from ..domain.models import TaskSnapshot


class FileRunMapper:
    """Read the existing atomic run artifacts without creating a second store."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        store: StateStore | None = None,
    ) -> None:
        self.store = store or StateStore(repo_root)

    def validate_task_id(self, task_id: str) -> None:
        self.store.run_dir(task_id)

    def unfinished_task_ids(self) -> list[str]:
        return self.store.unfinished_task_ids()

    def load_task(self, task_id: str) -> TaskSpec | None:
        run_dir = self.store.run_dir(task_id)
        if not (run_dir / "task.json").is_file():
            return None
        return self.store.load_task(task_id)

    def load_snapshot(self, task_id: str) -> TaskSnapshot | None:
        run_dir = self.store.run_dir(task_id)
        if not (run_dir / "task.json").is_file() or not (
            run_dir / "state.json"
        ).is_file():
            return None

        task = self.store.load_task(task_id)
        state = self.store.load_state(task_id)
        control = self.store.load_control(task_id)
        projected_status = state.status.value
        if control is not None and state.status.value == "running":
            projected_status = (
                "pausing" if control.get("action") == "pause" else "cancelling"
            )
        legacy = state.schema_version == 0
        manifest = self._optional_json(run_dir / "manifest.json")
        permissions = self._optional_json(run_dir / "permissions.json")
        changes = self._optional_json(run_dir / "changes/files.json")
        review_history = [
            review.to_dict() for review in self.store.load_review_history(task_id)
        ]
        review = (
            review_history[-1]
            if review_history
            else self._optional_json(run_dir / "review.json")
        )
        events = self._events(run_dir / "events.jsonl")
        context = {
            "generation": self._optional_json(run_dir / "context/generation.json"),
            "evaluation": self._optional_json(run_dir / "context/evaluation.json"),
        }
        evaluations = {
            "specification": self._optional_json(run_dir / "evaluations/spec.json"),
            "architecture": self._optional_json(
                run_dir / "evaluations/architecture.json"
            ),
            "aggregate": self._optional_json(run_dir / "evaluations/aggregate.json"),
        }
        commit = self._optional_json(run_dir / "delivery/commit.json")
        archive = {
            "summary": self._optional_json(run_dir / "archive/summary.json"),
            "outbox": self._optional_json(run_dir / "archive/outbox.json"),
        }
        repository = manifest.get("repository", {})
        workspace = (
            {
                "base_ref": repository.get("base_ref", state.base_ref),
                "base_commit": repository.get("base_commit", state.base_commit),
                "task_branch": repository.get("task_branch", state.task_branch),
                "worktree": repository.get(
                    "worktree_relative_path", state.worktree_relative_path
                ),
                "source_worktree_was_dirty": repository.get(
                    "source_worktree_was_dirty", state.source_worktree_was_dirty
                ),
            }
            if not legacy
            else {}
        )
        final_diff = changes.get("final_diff", {})
        return TaskSnapshot(
            task_id=task.task_id,
            requirement=task.requirement,
            acceptance_criteria=list(task.acceptance_criteria),
            status=projected_status,
            schema_version=state.schema_version,
            legacy=legacy,
            history_warning=(
                "历史记录不完整：该任务创建于隔离、权限和完整审计启用之前。"
                if legacy
                else None
            ),
            machine_status=projected_status,
            review_status=(
                "unavailable" if legacy else state.review_status.value
            ),
            delivery_status=(
                "unavailable" if legacy else state.delivery_status.value
            ),
            phase=state.phase.value,
            thread_id=state.thread_id,
            turn_count=state.turn_count,
            failure_count=state.failure_count,
            cycle_turn_count=state.cycle_turn_count,
            cycle_failure_count=state.cycle_failure_count,
            rounds=[self._round_summary(item) for item in state.rounds],
            last_error_summary=state.last_error_summary,
            infrastructure_error=state.infrastructure_error,
            started_at=state.started_at,
            updated_at=state.updated_at,
            finished_at=state.finished_at,
            report_url=(
                f"/api/tasks/{task.task_id}/report"
                if (run_dir / "report.md").is_file()
                else None
            ),
            diff_url=(
                f"/api/tasks/{task.task_id}/diff"
                if (run_dir / "changes/final.diff").is_file()
                else None
            ),
            workspace=workspace,
            permissions=permissions,
            audit_summary={
                "event_count": len(events),
                "denied_event_count": sum(
                    event.get("type") == "permission.denied" for event in events
                ),
            },
            changed_files=list(changes.get("files", [])),
            codex_responses=self._responses(run_dir),
            final_diff_sha256=str(
                final_diff.get("raw_sha256", state.last_diff_sha256)
            ),
            diff_redaction_count=int(
                final_diff.get("redaction_count", state.diff_redaction_count) or 0
            ),
            review=review or None,
            review_history=review_history,
            context={key: value for key, value in context.items() if value},
            evaluations={
                key: value for key, value in evaluations.items() if value
            },
            commit=commit,
            archive={key: value for key, value in archive.items() if value},
            queue_id=task.queue_id,
            sequence=task.sequence,
            rerun_of=task.rerun_of,
        )

    def load_report(self, task_id: str) -> str | None:
        path = self.store.run_dir(task_id) / "report.md"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def load_diff(self, task_id: str) -> str | None:
        path = self.store.run_dir(task_id) / "changes/final.diff"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _optional_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _events(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        return events

    @staticmethod
    def _responses(run_dir: Path) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        for path in sorted((run_dir / "turns").glob("turn-*/response.md")):
            try:
                turn_number = int(path.parent.name.removeprefix("turn-"))
            except ValueError:
                continue
            responses.append(
                {
                    "turn_number": turn_number,
                    "response": path.read_text(encoding="utf-8"),
                }
            )
        return responses

    @staticmethod
    def _round_summary(validation_round: ValidationRound) -> dict[str, Any]:
        return {
            "round_number": validation_round.round_number,
            "passed": validation_round.passed,
            "stage": validation_round.stage,
            "started_at": validation_round.started_at,
            "finished_at": validation_round.finished_at,
            "failure_summary": validation_round.failure_summary,
            "infrastructure_error": validation_round.infrastructure_error,
            "commands": [
                FileRunMapper._command_summary(result)
                for result in validation_round.command_results
            ],
        }

    @staticmethod
    def _command_summary(result: CommandResult) -> dict[str, Any]:
        return {
            "command": list(result.command),
            "stage": result.stage,
            "duration_seconds": result.duration_seconds,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "infrastructure_error": result.infrastructure_error,
            "log_path": result.log_path,
            "log_sha256": result.log_sha256,
            "passed": result.passed,
        }
