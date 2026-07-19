from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from orchestrator.codex_loop.queue_workflow import QueueWorkflow
from orchestrator.codex_loop.models import (
    QueueStatus,
    QueueTaskStatus,
    generate_queue_id,
    utc_now_iso,
    TaskQueueSpec,
)
from orchestrator.codex_loop.state import ActiveRunError, StateStore

from ..domain.models import QueueSnapshot
from ..exceptions.business_exception import (
    InvalidQueueIdError,
    QueueNotFoundError,
    QueueNotReadyError,
    TaskConflictError,
)
from ..mapper.file_queue import FileQueueMapper
from ..utils.task_executor import TaskExecutor


QueueWorkflowFactory = Callable[[], QueueWorkflow]


class QueueService:
    """HTTP-facing service for durable, strictly ordered task queues."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        validation_timeout_seconds: float = 900.0,
        executor: TaskExecutor | None = None,
        mapper: FileQueueMapper | None = None,
        workflow_factory: QueueWorkflowFactory | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.executor = executor or TaskExecutor()
        self.mapper = mapper or FileQueueMapper(self.repo_root)
        self.store = self.mapper.store
        self.workflow_factory = workflow_factory or (
            lambda: QueueWorkflow(
                self.repo_root,
                validation_timeout_seconds=validation_timeout_seconds,
            )
        )
        self._submission_lock = RLock()

    def start_queue(
        self,
        name: str,
        subtasks: list[Mapping[str, Any]],
        *,
        rerun_of: str | None = None,
    ) -> QueueSnapshot:
        with self._submission_lock:
            self._ensure_available()
            workflow = self.workflow_factory()
            queue_id = generate_queue_id()
            try:
                state, _ = self.executor.prepare_and_submit(
                    queue_id,
                    lambda: workflow.prepare(
                        name,
                        subtasks,
                        queue_id=queue_id,
                        rerun_of=rerun_of,
                    ),
                    lambda prepared: workflow.run_current(prepared.queue_id),
                )
            except (ActiveRunError, RuntimeError) as exc:
                raise TaskConflictError(str(exc)) from exc
        snapshot = self.mapper.load_snapshot(state.queue_id)
        if snapshot is None:  # pragma: no cover - prepare persists both files
            raise QueueNotFoundError(state.queue_id)
        return snapshot

    def start_spec(self, spec: TaskQueueSpec) -> QueueSnapshot:
        """Start one immutable queue produced by a confirmed Plan."""

        with self._submission_lock:
            self._ensure_available()
            workflow = self.workflow_factory()
            try:
                state, _ = self.executor.prepare_and_submit(
                    spec.queue_id,
                    lambda: workflow.prepare_spec(spec),
                    lambda prepared: workflow.run_current(prepared.queue_id),
                )
            except (ActiveRunError, RuntimeError) as exc:
                raise TaskConflictError(str(exc)) from exc
        snapshot = self.mapper.load_snapshot(state.queue_id)
        if snapshot is None:
            raise QueueNotFoundError(state.queue_id)
        return snapshot

    def get_queue(self, queue_id: str) -> QueueSnapshot:
        self._validate_queue_id(queue_id)
        snapshot = self.mapper.load_snapshot(queue_id)
        if snapshot is None:
            raise QueueNotFoundError(queue_id)
        return snapshot

    def resume_queue(self, queue_id: str) -> QueueSnapshot:
        snapshot = self.get_queue(queue_id)
        if snapshot.status in {"completed", "cancelled", "rejected", "waiting_review"}:
            return snapshot
        with self._submission_lock:
            if self.executor.active_task_id() is not None:
                raise TaskConflictError(
                    f"Another task is already running: {self.executor.active_task_id()}"
                )
            workflow = self.workflow_factory()
            try:
                self.executor.submit_operation(
                    queue_id,
                    lambda: workflow.resume(queue_id),
                )
            except RuntimeError as exc:
                raise TaskConflictError(str(exc)) from exc
        return snapshot

    def request_control(self, queue_id: str, action: str) -> QueueSnapshot:
        snapshot = self.get_queue(queue_id)
        normalized = str(action).strip().lower()
        if normalized not in {"pause", "cancel"}:
            raise TaskConflictError("Unsupported queue control action")
        state = self.store.load_state(queue_id)
        if state.status in {
            QueueStatus.COMPLETED,
            QueueStatus.CANCELLED,
            QueueStatus.REJECTED,
        }:
            raise TaskConflictError(
                f"Queue {queue_id} cannot be {normalized}d from {state.status.value}"
            )
        if normalized == "pause" and state.status is QueueStatus.WAITING_REVIEW:
            raise TaskConflictError("A queue waiting for review has no active work to pause")
        if normalized == "pause" and state.status is QueueStatus.PAUSED:
            return snapshot

        active = self.executor.active_task_id() == queue_id
        if active:
            self.store.request_control(queue_id, normalized)
            if state.current_task_id:
                child_store = self.store.subtask_store(queue_id)
                child_dir = child_store.run_dir(state.current_task_id)
                if (child_dir / "state.json").is_file():
                    child_store.request_control(state.current_task_id, normalized)
        else:
            child = (
                state.task(state.current_task_id)
                if state.current_task_id is not None
                else None
            )
            if normalized == "pause":
                state.status = QueueStatus.PAUSED
                if child is not None and child.status is QueueTaskStatus.RUNNING:
                    child.status = QueueTaskStatus.PAUSED
                    child_store = self.store.subtask_store(queue_id)
                    if (child_store.run_dir(child.task_id) / "state.json").is_file():
                        run_state = child_store.load_state(child.task_id)
                        run_state.mark_paused()
                        child_store.save_state(run_state)
                event_type = "queue.paused"
            else:
                state.status = QueueStatus.CANCELLED
                state.finished_at = utc_now_iso()
                if child is not None and child.status not in {
                    QueueTaskStatus.COMPLETED,
                    QueueTaskStatus.SKIPPED,
                }:
                    child.status = QueueTaskStatus.CANCELLED
                event_type = "queue.cancelled"
            self.store.clear_control(queue_id)
            self.store.save_state(state)
            self.store.append_event(
                queue_id,
                event_type,
                {"task_id": state.current_task_id, "inactive_executor": True},
            )
        updated = self.mapper.load_snapshot(queue_id)
        if updated is None:  # pragma: no cover - checked by get_queue above
            raise QueueNotFoundError(queue_id)
        return updated

    def rerun_queue(self, queue_id: str) -> QueueSnapshot:
        snapshot = self.get_queue(queue_id)
        if snapshot.status in {"pending", "running", "pausing", "paused", "cancelling", "waiting_review"}:
            raise TaskConflictError("An unfinished queue cannot be rerun")
        spec = self.store.load_spec(queue_id)
        return self.start_queue(
            spec.name,
            [
                {
                    "requirement": task.requirement,
                    "acceptance_criteria": list(task.acceptance_criteria),
                }
                for task in spec.subtasks
            ],
            rerun_of=queue_id,
        )

    def skip_subtask(
        self,
        queue_id: str,
        task_id: str,
        *,
        expected_updated_at: str | None = None,
    ) -> QueueSnapshot:
        snapshot = self.get_queue(queue_id)
        self._assert_version(snapshot, expected_updated_at)
        if self.executor.active_task_id() == queue_id:
            raise TaskConflictError("Wait for the current execution checkpoint before skipping")
        state = self.store.load_state(queue_id)
        child = state.task(task_id)
        if child.status is not QueueTaskStatus.PENDING:
            raise TaskConflictError("Only a pending subtask can be skipped")
        child.status = QueueTaskStatus.SKIPPED
        child.updated_at = utc_now_iso()
        if state.current_task_id is None and all(
            item.status in {QueueTaskStatus.COMPLETED, QueueTaskStatus.SKIPPED}
            for item in state.subtasks
        ):
            state.status = QueueStatus.COMPLETED
            state.finished_at = utc_now_iso()
        self.store.save_state(state)
        self.store.append_event(
            queue_id,
            "subtask.skipped",
            {"task_id": task_id, "sequence": child.sequence},
        )
        updated = self.mapper.load_snapshot(queue_id)
        if updated is None:  # pragma: no cover
            raise QueueNotFoundError(queue_id)
        return updated

    def reorder_pending(
        self,
        queue_id: str,
        task_ids: list[str],
        *,
        expected_updated_at: str | None = None,
    ) -> QueueSnapshot:
        snapshot = self.get_queue(queue_id)
        self._assert_version(snapshot, expected_updated_at)
        if self.executor.active_task_id() == queue_id:
            raise TaskConflictError("Wait for the current execution checkpoint before reordering")
        state = self.store.load_state(queue_id)
        pending = [
            item for item in state.ordered_subtasks()
            if item.status is QueueTaskStatus.PENDING
        ]
        if len(task_ids) != len(pending) or set(task_ids) != {
            item.task_id for item in pending
        }:
            raise TaskConflictError("Reorder must contain every pending subtask exactly once")
        slots = sorted(item.sequence for item in pending)
        by_id = {item.task_id: item for item in pending}
        for sequence, task_id in zip(slots, task_ids, strict=True):
            by_id[task_id].sequence = sequence
            by_id[task_id].updated_at = utc_now_iso()
        self.store.save_state(state)
        self.store.append_event(
            queue_id,
            "queue.reordered",
            {"task_ids": list(task_ids)},
        )
        updated = self.mapper.load_snapshot(queue_id)
        if updated is None:  # pragma: no cover
            raise QueueNotFoundError(queue_id)
        return updated

    def get_report(self, queue_id: str) -> str:
        self.get_queue(queue_id)
        report = self.mapper.load_report(queue_id)
        if report is None:
            raise QueueNotReadyError(queue_id)
        return report

    def get_diff(self, queue_id: str) -> str:
        self.get_queue(queue_id)
        diff = self.mapper.load_diff(queue_id)
        if diff is None:
            raise QueueNotReadyError(queue_id)
        return diff

    def _ensure_available(self) -> None:
        active = self.executor.active_task_id()
        if active is not None:
            raise TaskConflictError(f"Another task is already running: {active}")
        unfinished_tasks = StateStore(self.repo_root).unfinished_task_ids()
        if unfinished_tasks:
            raise TaskConflictError(
                "An unfinished task must be resumed before starting a queue: "
                + ", ".join(unfinished_tasks)
            )
        unfinished_queues = self.mapper.unfinished_queue_ids()
        if unfinished_queues:
            raise TaskConflictError(
                "An unfinished queue must be resumed before starting another queue: "
                + ", ".join(unfinished_queues)
            )

    def _validate_queue_id(self, queue_id: str) -> None:
        try:
            self.mapper.validate_queue_id(queue_id)
        except (TypeError, ValueError):
            raise InvalidQueueIdError() from None

    @staticmethod
    def _assert_version(
        snapshot: QueueSnapshot,
        expected_updated_at: str | None,
    ) -> None:
        if expected_updated_at and snapshot.updated_at != expected_updated_at:
            raise TaskConflictError(
                "Queue changed after it was displayed; refresh before editing"
            )
