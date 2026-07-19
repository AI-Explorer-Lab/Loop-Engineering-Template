from __future__ import annotations

from pathlib import Path

from codex_loop.state import QueueStore

from ..domain.models import QueueSnapshot, QueueSubtaskSnapshot


class FileQueueMapper:
    """Aggregate one durable queue and its nested subtask projections."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        store: QueueStore | None = None,
    ) -> None:
        self.store = store or QueueStore(repo_root)

    def validate_queue_id(self, queue_id: str) -> None:
        self.store.queue_dir(queue_id)

    def unfinished_queue_ids(self) -> list[str]:
        return self.store.unfinished_queue_ids()

    def load_snapshot(self, queue_id: str) -> QueueSnapshot | None:
        queue_dir = self.store.queue_dir(queue_id)
        if not (queue_dir / "queue.json").is_file() or not (
            queue_dir / "state.json"
        ).is_file():
            return None
        spec = self.store.load_spec(queue_id)
        state = self.store.load_state(queue_id)
        control = self.store.load_control(queue_id)
        projected_status = state.status.value
        projected_child_status: str | None = None
        if control is not None and state.status.value in {"pending", "running"}:
            projected_status = (
                "pausing" if control.get("action") == "pause" else "cancelling"
            )
            projected_child_status = projected_status
        specs_by_id = {task.task_id: task for task in spec.subtasks}
        subtasks: list[QueueSubtaskSnapshot] = []
        for child in state.ordered_subtasks():
            task = specs_by_id[child.task_id]
            subtasks.append(
                QueueSubtaskSnapshot(
                    task_id=child.task_id,
                    sequence=child.sequence,
                    requirement=task.requirement,
                    acceptance_criteria=list(task.acceptance_criteria),
                    status=(
                        projected_child_status
                        if child.task_id == state.current_task_id
                        and projected_child_status is not None
                        else child.status.value
                    ),
                    machine_status=(
                        None
                        if child.machine_status is None
                        else child.machine_status.value
                    ),
                    review_status=child.review_status.value,
                    delivery_status=child.delivery_status.value,
                    thread_id=child.thread_id,
                    last_error_summary=child.last_error_summary,
                    updated_at=child.updated_at,
                )
            )
        delivery_status = "not_ready"
        if state.current_task_id is not None:
            delivery_status = state.task(state.current_task_id).delivery_status.value
        elif state.subtasks:
            statuses = [item.delivery_status.value for item in state.subtasks]
            if all(value == "archived" for value in statuses):
                delivery_status = "archived"
            elif any(value == "failed" for value in statuses):
                delivery_status = "failed"
            elif any(value in {"committed", "archive_pending", "archived"} for value in statuses):
                delivery_status = "committed"
        return QueueSnapshot(
            queue_id=queue_id,
            name=spec.name,
            status=projected_status,
            base_ref=state.base_ref,
            base_commit=state.base_commit,
            current_task_id=state.current_task_id,
            cumulative_diff_sha256=state.cumulative_diff_sha256,
            last_error_summary=state.last_error_summary,
            delivery_status=delivery_status,
            subtasks=subtasks,
            started_at=state.started_at,
            updated_at=state.updated_at,
            finished_at=state.finished_at,
            report_url=(
                f"/api/queues/{queue_id}/report"
                if (queue_dir / "report.md").is_file()
                else None
            ),
            diff_url=(
                f"/api/queues/{queue_id}/diff"
                if self.store.cumulative_diff_path(queue_id).is_file()
                else None
            ),
            rerun_of=spec.rerun_of,
        )

    def load_report(self, queue_id: str) -> str | None:
        path = self.store.queue_dir(queue_id) / "report.md"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def load_diff(self, queue_id: str) -> str | None:
        path = self.store.cumulative_diff_path(queue_id)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
