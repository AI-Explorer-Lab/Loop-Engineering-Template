from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any, Callable

from codex_loop.models import DeliveryStatus, QueueStatus, TaskSpec
from codex_loop.queue_workflow import QueueWorkflow
from codex_loop.review import ReviewError, ReviewService
from codex_loop.git_delivery import DeliveryError, GitDeliveryService
from codex_loop.state import QueueStore, redact_sensitive_text
from codex_loop.workflow import OrchestrationWorkflow

from ..constant.enums import ApiTaskStatus
from ..domain.models import TaskSnapshot
from ..exceptions.business_exception import (
    InvalidTaskIdError,
    ReviewConflictError,
    TaskConflictError,
    TaskNotFoundError,
    TaskNotReadyError,
)
from ..mapper.file_run import FileRunMapper
from ..utils.task_executor import TaskExecutor


WorkflowFactory = Callable[[], OrchestrationWorkflow]
QueueWorkflowFactory = Callable[[], QueueWorkflow]
ArchiveCallback = Callable[[str, Any], dict[str, Any]]


class TaskService:
    """Bridge the HTTP API to the existing blocking orchestration workflow."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        validation_timeout_seconds: float = 900.0,
        executor: TaskExecutor | None = None,
        mapper: FileRunMapper | None = None,
        workflow_factory: WorkflowFactory | None = None,
        review_service: ReviewService | None = None,
        queue_workflow_factory: QueueWorkflowFactory | None = None,
        delivery_service: GitDeliveryService | None = None,
        archive_callback: ArchiveCallback | None = None,
        archive_retry_callback: ArchiveCallback | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.executor = executor or TaskExecutor()
        self.mapper = mapper or FileRunMapper(self.repo_root)
        self.workflow_factory = workflow_factory or (
            lambda: OrchestrationWorkflow(
                self.repo_root,
                validation_timeout_seconds=validation_timeout_seconds,
            )
        )
        self.review_service = review_service or ReviewService(self.repo_root)
        self.delivery_service = delivery_service or GitDeliveryService(self.repo_root)
        self.archive_callback = archive_callback
        self.archive_retry_callback = archive_retry_callback
        self.queue_store = QueueStore(self.repo_root)
        self.queue_workflow_factory = queue_workflow_factory or (
            lambda: QueueWorkflow(
                self.repo_root,
                validation_timeout_seconds=validation_timeout_seconds,
            )
        )
        self._submission_lock = RLock()
        self._pending_archive_jobs: set[str] = set()

    def start_task(
        self,
        requirement: str,
        acceptance_criteria: list[str],
        *,
        rerun_of: str | None = None,
    ) -> TaskSnapshot:
        task = TaskSpec(
            requirement=requirement,
            acceptance_criteria=acceptance_criteria,
            rerun_of=rerun_of,
        )
        with self._submission_lock:
            self._ensure_available()
            try:
                self.executor.submit(
                    task,
                    lambda: self.workflow_factory().start(task),
                )
            except RuntimeError as exc:
                raise TaskConflictError(str(exc)) from exc
        return self._accepted_snapshot(task)

    def start_spec(self, task: TaskSpec) -> TaskSnapshot:
        """Start one immutable TaskSpec produced by a confirmed Plan."""

        with self._submission_lock:
            self._ensure_available()
            try:
                self.executor.submit(
                    task,
                    lambda: self.workflow_factory().start(task),
                )
            except RuntimeError as exc:
                raise TaskConflictError(str(exc)) from exc
        return self._accepted_snapshot(task)

    def get_task(self, task_id: str) -> TaskSnapshot:
        self._validate_task_id(task_id)
        mapper, queue_id = self._mapper_for_task(task_id)
        snapshot = mapper.load_snapshot(task_id)
        if snapshot is not None:
            if (
                queue_id is None
                and snapshot.review_status == "approved"
                and snapshot.delivery_status in {"committed", "archive_pending"}
            ):
                self._schedule_archive_recovery(task_id, mapper.store)
                return mapper.load_snapshot(task_id) or snapshot
            return snapshot

        record = self.executor.get(task_id)
        if record is None:
            raise TaskNotFoundError(task_id)
        if not record.future.done():
            if record.task is not None:
                accepted = self._accepted_snapshot(record.task)
                control = mapper.store.load_control(task_id)
                if control is None:
                    return accepted
                values = accepted.to_dict()
                projected = (
                    "pausing"
                    if control.get("action") == "pause"
                    else "cancelling"
                )
                values.update(status=projected, machine_status=projected)
                return TaskSnapshot(**values)
            raise TaskNotFoundError(task_id)

        error = record.future.exception()
        if error is not None:
            message = redact_sensitive_text(str(error) or type(error).__name__)
            if record.task is None:
                raise TaskNotFoundError(task_id)
            values = self._accepted_snapshot(record.task).to_dict()
            values.update(
                status=ApiTaskStatus.INFRASTRUCTURE_ERROR.value,
                infrastructure_error=message,
                last_error_summary=message,
            )
            return TaskSnapshot(**values)
        raise TaskNotFoundError(task_id)

    def resume_task(self, task_id: str) -> TaskSnapshot:
        self._validate_task_id(task_id)
        mapper, queue_id = self._mapper_for_task(task_id)
        if queue_id is not None:
            raise TaskConflictError(
                f"Queued subtask must be resumed through queue {queue_id}"
            )
        task = mapper.load_task(task_id)
        snapshot = mapper.load_snapshot(task_id)
        if task is None or snapshot is None:
            raise TaskNotFoundError(task_id)
        if snapshot.status not in {ApiTaskStatus.RUNNING.value, "paused"}:
            return snapshot

        with self._submission_lock:
            active_task_id = self.executor.active_task_id()
            if active_task_id is not None:
                raise TaskConflictError(
                    f"Another task is already running: {active_task_id}"
                )
            try:
                self.executor.submit(
                    task,
                    lambda: self.workflow_factory().resume(task_id),
                )
            except RuntimeError as exc:
                raise TaskConflictError(str(exc)) from exc
        return snapshot

    def request_control(self, task_id: str, action: str) -> TaskSnapshot:
        self._validate_task_id(task_id)
        mapper, queue_id = self._mapper_for_task(task_id)
        if queue_id is not None:
            raise TaskConflictError(
                f"Queued subtask must be controlled through queue {queue_id}"
            )
        task = mapper.load_task(task_id)
        snapshot = mapper.load_snapshot(task_id)
        normalized = str(action).strip().lower()
        if normalized not in {"pause", "cancel"}:
            raise TaskConflictError("Unsupported task control action")
        if task is None or snapshot is None:
            record = self.executor.get(task_id)
            if (
                record is None
                or record.future.done()
                or record.task is None
            ):
                raise TaskNotFoundError(task_id)
            mapper.store.request_control(task_id, normalized)
            accepted = self._accepted_snapshot(record.task)
            values = accepted.to_dict()
            projected = "pausing" if normalized == "pause" else "cancelling"
            values.update(status=projected, machine_status=projected)
            return TaskSnapshot(**values)
        state = mapper.store.load_state(task_id)
        if state.status.value in {
            "success",
            "manual_review",
            "infrastructure_error",
            "cancelled",
        }:
            raise TaskConflictError(
                f"Task {task_id} cannot be {normalized}d from {state.status.value}"
            )
        if state.status.value == "paused" and normalized == "pause":
            return snapshot
        active = self.executor.active_task_id() == task_id
        if active:
            mapper.store.request_control(task_id, normalized)
        else:
            if normalized == "pause":
                state.mark_paused()
                event_type = "run.paused"
            else:
                state.mark_cancelled()
                event_type = "run.cancelled"
            mapper.store.clear_control(task_id)
            mapper.store.save_state(state)
            mapper.store.append_event(
                task_id,
                event_type,
                {"checkpoint": state.phase.value, "inactive_executor": True},
            )
        updated = mapper.load_snapshot(task_id)
        if updated is None:  # pragma: no cover - persisted task was checked above
            raise TaskNotFoundError(task_id)
        return updated

    def rerun_task(self, task_id: str) -> TaskSnapshot:
        self._validate_task_id(task_id)
        mapper, queue_id = self._mapper_for_task(task_id)
        if queue_id is not None:
            raise TaskConflictError(
                f"Queued subtask must be rerun through queue {queue_id}"
            )
        task = mapper.load_task(task_id)
        snapshot = mapper.load_snapshot(task_id)
        if task is None or snapshot is None:
            raise TaskNotFoundError(task_id)
        if snapshot.status in {"running", "pausing", "paused", "cancelling"}:
            raise TaskConflictError("An unfinished task cannot be rerun")
        return self.start_task(
            task.requirement,
            list(task.acceptance_criteria),
            rerun_of=task_id,
        )

    def get_report(self, task_id: str) -> str:
        self._validate_task_id(task_id)
        mapper, _ = self._mapper_for_task(task_id)
        task = mapper.load_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        report = mapper.load_report(task_id)
        if report is None:
            raise TaskNotReadyError(task_id)
        return report

    def get_diff(self, task_id: str) -> str:
        self._validate_task_id(task_id)
        mapper, _ = self._mapper_for_task(task_id)
        task = mapper.load_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        diff = mapper.load_diff(task_id)
        if diff is None:
            raise TaskNotReadyError(task_id)
        return diff

    def review_task(
        self,
        task_id: str,
        *,
        decision: str,
        reviewer: str,
        comment: str,
        reviewed_diff_sha256: str,
        commit_subject: str = "",
    ) -> TaskSnapshot:
        self._validate_task_id(task_id)
        mapper, queue_id = self._mapper_for_task(task_id)
        if mapper.load_task(task_id) is None:
            raise TaskNotFoundError(task_id)
        try:
            if queue_id is None:
                review = self.review_service.record(
                    task_id,
                    decision=decision,
                    reviewer=reviewer,
                    comment=comment,
                    reviewed_diff_sha256=reviewed_diff_sha256,
                    commit_subject=commit_subject,
                )
                if review.decision.value == "approved":
                    self.delivery_service.deliver(task_id, review=review)
                    self._schedule_archive_after_commit(task_id, mapper.store)
            else:
                workflow = self.queue_workflow_factory()
                queue_state, _ = workflow.record_review(
                    queue_id,
                    task_id,
                    decision=decision,
                    reviewer=reviewer,
                    comment=comment,
                    reviewed_diff_sha256=reviewed_diff_sha256,
                    commit_subject=commit_subject,
                )
                if decision == "approved":
                    child_store = workflow.queue_store.subtask_store(queue_id)
                    refreshed_child = child_store.load_state(task_id)
                    refreshed_queue = workflow.queue_store.load_state(queue_id)
                    refreshed_queue.task(task_id).delivery_status = (
                        refreshed_child.delivery_status
                    )
                    workflow.queue_store.save_state(refreshed_queue)
                    queue_state = refreshed_queue
                if queue_state.status in {QueueStatus.PENDING, QueueStatus.RUNNING}:
                    self.executor.submit_operation(
                        queue_id,
                        lambda: workflow.run_current(queue_id),
                    )
        except (ReviewError, DeliveryError, RuntimeError, ValueError) as exc:
            raise ReviewConflictError(str(exc)) from exc
        snapshot = mapper.load_snapshot(task_id)
        if snapshot is None:  # pragma: no cover - guarded by persisted task/state
            raise TaskNotFoundError(task_id)
        return snapshot

    def retry_commit(self, task_id: str) -> TaskSnapshot:
        self._validate_task_id(task_id)
        mapper, queue_id = self._mapper_for_task(task_id)
        if mapper.load_task(task_id) is None:
            raise TaskNotFoundError(task_id)
        review = (
            mapper.store.load_latest_review(task_id)
            if queue_id is not None
            else mapper.store.load_review(task_id)
        )
        try:
            if queue_id is None:
                GitDeliveryService(self.repo_root, store=mapper.store).deliver(
                    task_id, review=review
                )
                self._schedule_archive_after_commit(task_id, mapper.store)
            else:
                workflow = self.queue_workflow_factory()
                queue_state = workflow.recover_approved_delivery(
                    queue_id, task_id
                )
                if queue_state.status in {QueueStatus.PENDING, QueueStatus.RUNNING}:
                    self.executor.submit_operation(
                        queue_id,
                        lambda: workflow.run_current(queue_id),
                    )
        except (DeliveryError, RuntimeError, ValueError) as exc:
            raise ReviewConflictError(str(exc)) from exc
        snapshot = mapper.load_snapshot(task_id)
        if snapshot is None:
            raise TaskNotFoundError(task_id)
        return snapshot

    def retry_archive(self, task_id: str) -> TaskSnapshot:
        self._validate_task_id(task_id)
        mapper, queue_id = self._mapper_for_task(task_id)
        if mapper.load_task(task_id) is None:
            raise TaskNotFoundError(task_id)
        if queue_id is None:
            callback = self._archive_recovery_callback(task_id, mapper.store)
            if callback is None:
                raise ReviewConflictError("archive capability is unavailable")
            self._schedule_archive(
                task_id,
                mapper.store,
                callback=callback,
            )
        else:
            if self.archive_retry_callback is None:
                raise ReviewConflictError("archive capability is unavailable")
            try:
                self.archive_retry_callback(task_id, store=mapper.store)
            except Exception as exc:
                raise ReviewConflictError(str(exc)) from exc
        snapshot = mapper.load_snapshot(task_id)
        if snapshot is None:
            raise TaskNotFoundError(task_id)
        return snapshot

    def close(self, *, wait: bool = False) -> None:
        self.executor.shutdown(wait=wait)

    def _schedule_archive_after_commit(self, task_id: str, store: Any) -> None:
        if self.archive_callback is None:
            return
        self._schedule_archive(task_id, store, callback=self.archive_callback)

    def _schedule_archive_recovery(self, task_id: str, store: Any) -> None:
        callback = self._archive_recovery_callback(task_id, store)
        if callback is None:
            return
        self._schedule_archive(task_id, store, callback=callback)

    def _archive_recovery_callback(
        self,
        task_id: str,
        store: Any,
    ) -> ArchiveCallback | None:
        outbox_path = store.run_dir(task_id) / "archive" / "outbox.json"
        if outbox_path.is_file():
            return self.archive_retry_callback or self.archive_callback
        return self.archive_callback

    def _schedule_archive(
        self,
        task_id: str,
        store: Any,
        *,
        callback: ArchiveCallback,
    ) -> None:
        with self._submission_lock:
            if task_id in self._pending_archive_jobs:
                return
            state = store.load_state(task_id)
            state.delivery_status = DeliveryStatus.ARCHIVE_PENDING
            state.last_error_summary = ""
            store.save_state(state)
            self._pending_archive_jobs.add(task_id)
            self._submit_archive_when_available(task_id, store, callback)

    def _submit_archive_when_available(
        self,
        task_id: str,
        store: Any,
        callback: ArchiveCallback,
    ) -> None:
        active_identifier = self.executor.active_task_id()
        if active_identifier is not None:
            active = self.executor.get(active_identifier)
            if active is None:  # pragma: no cover - guarded by TaskExecutor's lock
                self._pending_archive_jobs.discard(task_id)
                self._mark_archive_failed(
                    task_id,
                    store,
                    RuntimeError("active executor operation is unavailable"),
                )
                return
            active.future.add_done_callback(
                lambda _future: self._resume_archive_submission(
                    task_id,
                    store,
                    callback,
                )
            )
            return
        try:
            self.executor.submit_operation(
                task_id,
                lambda: self._run_archive_job(task_id, store, callback),
            )
        except RuntimeError as exc:
            active_identifier = self.executor.active_task_id()
            active = (
                None
                if active_identifier is None
                else self.executor.get(active_identifier)
            )
            if active is not None:
                active.future.add_done_callback(
                    lambda _future: self._resume_archive_submission(
                        task_id,
                        store,
                        callback,
                    )
                )
                return
            self._pending_archive_jobs.discard(task_id)
            self._mark_archive_failed(task_id, store, exc)

    def _resume_archive_submission(
        self,
        task_id: str,
        store: Any,
        callback: ArchiveCallback,
    ) -> None:
        with self._submission_lock:
            if task_id not in self._pending_archive_jobs:
                return
            self._submit_archive_when_available(task_id, store, callback)

    def _run_archive_job(
        self,
        task_id: str,
        store: Any,
        callback: ArchiveCallback,
    ) -> None:
        try:
            self._invoke_archive_callback(task_id, store, callback)
            state = store.load_state(task_id)
            if state.delivery_status not in {
                DeliveryStatus.ARCHIVED,
                DeliveryStatus.FAILED,
            }:
                self._mark_archive_failed(
                    task_id,
                    store,
                    RuntimeError(
                        "archive callback completed without a terminal delivery status"
                    ),
                )
        finally:
            with self._submission_lock:
                self._pending_archive_jobs.discard(task_id)

    def _archive_after_commit(self, task_id: str, store: Any) -> None:
        if self.archive_callback is None:
            return
        self._invoke_archive_callback(task_id, store, self.archive_callback)

    def _invoke_archive_callback(
        self,
        task_id: str,
        store: Any,
        callback: ArchiveCallback,
    ) -> None:
        try:
            callback(task_id, store=store)
        except Exception as exc:
            self._mark_archive_failed(task_id, store, exc)

    @staticmethod
    def _mark_archive_failed(task_id: str, store: Any, error: Exception) -> None:
        state = store.load_state(task_id)
        state.delivery_status = DeliveryStatus.FAILED
        state.last_error_summary = redact_sensitive_text(
            str(error) or type(error).__name__
        )
        store.save_state(state)
        store.append_event(
            task_id,
            "knowledge.write_failed",
            {"error": state.last_error_summary},
        )

    def _ensure_available(self) -> None:
        active_task_id = self.executor.active_task_id()
        if active_task_id is not None:
            raise TaskConflictError(f"Another task is already running: {active_task_id}")
        unfinished = self.mapper.unfinished_task_ids()
        if unfinished:
            raise TaskConflictError(
                "An unfinished task must be resumed before starting another task: "
                + ", ".join(unfinished)
            )
        unfinished_queues = self.queue_store.unfinished_queue_ids()
        if unfinished_queues:
            raise TaskConflictError(
                "An unfinished queue must be resumed before starting another task: "
                + ", ".join(unfinished_queues)
            )

    def _mapper_for_task(self, task_id: str) -> tuple[FileRunMapper, str | None]:
        if self.mapper.load_task(task_id) is not None:
            return self.mapper, None
        queue_id = self.queue_store.find_queue_for_task(task_id)
        if queue_id is None:
            return self.mapper, None
        return (
            FileRunMapper(
                self.repo_root,
                store=self.queue_store.subtask_store(queue_id),
            ),
            queue_id,
        )

    def _validate_task_id(self, task_id: str) -> None:
        try:
            self.mapper.validate_task_id(task_id)
        except (TypeError, ValueError):
            raise InvalidTaskIdError() from None

    @staticmethod
    def _accepted_snapshot(task: TaskSpec) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=task.task_id,
            requirement=task.requirement,
            acceptance_criteria=list(task.acceptance_criteria),
            status=ApiTaskStatus.ACCEPTED.value,
            started_at=task.created_at,
            updated_at=task.created_at,
            rerun_of=task.rerun_of,
        )
