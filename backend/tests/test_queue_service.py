from pathlib import Path

import pytest

from backend.exceptions.business_exception import TaskConflictError
from backend.service.queue_service import QueueService
from codex_loop.models import (
    QueueStatus,
    QueueTaskStatus,
    TaskQueueSpec,
)
from codex_loop.state import QueueStore


class CompletingQueueWorkflow:
    def __init__(self, store: QueueStore) -> None:
        self.store = store

    def prepare(
        self,
        name: str,
        subtasks: list[dict[str, object]],
        *,
        queue_id: str,
        rerun_of: str | None = None,
    ):
        spec = TaskQueueSpec.from_inputs(
            name,
            subtasks,
            queue_id=queue_id,
            rerun_of=rerun_of,
        )
        spec.base_commit = "a" * 40
        return self.store.initialize_queue(spec)

    def run_current(self, queue_id: str):
        state = self.store.load_state(queue_id)
        child = state.next_pending()
        if child is not None:
            child.status = QueueTaskStatus.WAITING_REVIEW
            state.current_task_id = child.task_id
        state.status = QueueStatus.WAITING_REVIEW
        self.store.save_state(state)
        self.store.save_report(queue_id, "# Queue report\n")
        return state

    def resume(self, queue_id: str):
        return self.run_current(queue_id)


def _subtasks() -> list[dict[str, object]]:
    return [
        {"requirement": "First", "acceptance_criteria": ["Works"]},
        {"requirement": "Second", "acceptance_criteria": ["Works"]},
    ]


def test_queue_service_persists_before_background_execution(tmp_path: Path) -> None:
    store = QueueStore(tmp_path)
    service = QueueService(
        tmp_path,
        workflow_factory=lambda: CompletingQueueWorkflow(store),
    )
    try:
        accepted = service.start_queue("Queue", _subtasks())
        record = service.executor.get(accepted.queue_id)
        assert record is not None
        record.future.result(timeout=5)

        completed_machine_step = service.get_queue(accepted.queue_id)
        assert accepted.status in {"pending", "waiting_review"}
        assert completed_machine_step.status == "waiting_review"
        assert completed_machine_step.current_task_id
        assert service.get_report(accepted.queue_id) == "# Queue report\n"
    finally:
        service.executor.shutdown(wait=True)


def test_queue_resume_uses_pending_file_state_without_a_memory_future(
    tmp_path: Path,
) -> None:
    store = QueueStore(tmp_path)
    spec = TaskQueueSpec.from_inputs(
        "Queue",
        _subtasks(),
        queue_id="queue-resume",
    )
    spec.base_commit = "a" * 40
    store.initialize_queue(spec)
    service = QueueService(
        tmp_path,
        workflow_factory=lambda: CompletingQueueWorkflow(store),
    )
    try:
        snapshot = service.resume_queue(spec.queue_id)
        record = service.executor.get(spec.queue_id)
        assert record is not None
        record.future.result(timeout=5)
    finally:
        service.executor.shutdown(wait=True)

    assert snapshot.status == "pending"
    assert store.load_state(spec.queue_id).status is QueueStatus.WAITING_REVIEW


def test_queue_can_reorder_and_skip_only_pending_subtasks(tmp_path: Path) -> None:
    store = QueueStore(tmp_path)
    spec = TaskQueueSpec.from_inputs(
        "Editable",
        [
            {"requirement": "First", "acceptance_criteria": ["Works"]},
            {"requirement": "Second", "acceptance_criteria": ["Works"]},
            {"requirement": "Third", "acceptance_criteria": ["Works"]},
        ],
        queue_id="queue-editable",
    )
    spec.base_commit = "a" * 40
    store.initialize_queue(spec)
    service = QueueService(tmp_path)
    try:
        reordered = service.reorder_pending(
            spec.queue_id,
            [spec.subtasks[2].task_id, spec.subtasks[1].task_id, spec.subtasks[0].task_id],
        )
        skipped = service.skip_subtask(spec.queue_id, spec.subtasks[2].task_id)

        assert [item.task_id for item in reordered.subtasks] == [
            spec.subtasks[2].task_id,
            spec.subtasks[1].task_id,
            spec.subtasks[0].task_id,
        ]
        assert skipped.subtasks[0].status == "skipped"
        with pytest.raises(TaskConflictError, match="refresh"):
            service.reorder_pending(
                spec.queue_id,
                [spec.subtasks[1].task_id, spec.subtasks[0].task_id],
                expected_updated_at="stale-version",
            )
        with pytest.raises(TaskConflictError, match="pending"):
            service.skip_subtask(spec.queue_id, spec.subtasks[2].task_id)
    finally:
        service.executor.shutdown(wait=True)


def test_inactive_queue_pause_resume_and_cancel_are_durable(tmp_path: Path) -> None:
    store = QueueStore(tmp_path)
    spec = TaskQueueSpec.from_inputs(
        "Controlled",
        _subtasks(),
        queue_id="queue-controlled",
    )
    spec.base_commit = "a" * 40
    store.initialize_queue(spec)
    service = QueueService(
        tmp_path,
        workflow_factory=lambda: CompletingQueueWorkflow(store),
    )
    try:
        paused = service.request_control(spec.queue_id, "pause")
        assert paused.status == "paused"
        assert spec.queue_id in store.unfinished_queue_ids()

        service.resume_queue(spec.queue_id)
        record = service.executor.get(spec.queue_id)
        assert record is not None
        record.future.result(timeout=5)
        assert service.get_queue(spec.queue_id).status == "waiting_review"

        cancelled_spec = TaskQueueSpec.from_inputs(
            "Cancelled",
            _subtasks(),
            queue_id="queue-cancelled",
        )
        cancelled_spec.base_commit = "a" * 40
        store.initialize_queue(cancelled_spec)
        cancelled = service.request_control(cancelled_spec.queue_id, "cancel")
        assert cancelled.status == "cancelled"
        assert cancelled.finished_at is not None
    finally:
        service.executor.shutdown(wait=True)


def test_infrastructure_error_queue_can_be_rerun_with_a_new_id(
    tmp_path: Path,
) -> None:
    store = QueueStore(tmp_path)
    spec = TaskQueueSpec.from_inputs(
        "Retry queue",
        _subtasks(),
        queue_id="queue-failed",
    )
    spec.base_commit = "a" * 40
    state = store.initialize_queue(spec)
    state.status = QueueStatus.INFRASTRUCTURE_ERROR
    state.last_error_summary = "runtime unavailable"
    store.save_state(state)
    service = QueueService(
        tmp_path,
        workflow_factory=lambda: CompletingQueueWorkflow(store),
    )
    try:
        rerun = service.rerun_queue(spec.queue_id)
        record = service.executor.get(rerun.queue_id)
        assert record is not None
        record.future.result(timeout=5)
    finally:
        service.executor.shutdown(wait=True)

    assert rerun.queue_id != spec.queue_id
    assert rerun.rerun_of == spec.queue_id
    assert rerun.name == spec.name
    assert store.load_state(spec.queue_id).status is QueueStatus.INFRASTRUCTURE_ERROR
