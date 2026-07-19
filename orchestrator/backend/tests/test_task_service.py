from __future__ import annotations

from pathlib import Path
from threading import BoundedSemaphore, Event

import pytest

from orchestrator.backend.exceptions.business_exception import (
    InvalidTaskIdError,
    TaskConflictError,
    TaskNotFoundError,
    TaskNotReadyError,
)
from orchestrator.backend.service.task_service import TaskService
from orchestrator.backend.utils.task_executor import TaskExecutor
from orchestrator.codex_loop.models import (
    DeliveryStatus,
    RunResult,
    TaskQueueSpec,
    TaskSpec,
)
from orchestrator.codex_loop.report import ReportBuilder
from orchestrator.codex_loop.state import QueueStore, StateStore


class CompletingWorkflow:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def start(self, task: TaskSpec) -> RunResult:
        state = self.store.initialize_run(task)
        state.thread_id = "thread-started"
        state.mark_success("M changed.py")
        self.store.save_state(state)
        result, report = ReportBuilder().build(task, state)
        self.store.save_result(result)
        self.store.save_report(task.task_id, report)
        return result

    def resume(self, task_id: str) -> RunResult:
        task = self.store.load_task(task_id)
        state = self.store.load_state(task_id)
        state.thread_id = state.thread_id or "thread-resumed"
        state.mark_success("M resumed.py")
        self.store.save_state(state)
        result, report = ReportBuilder().build(task, state)
        self.store.save_result(result)
        self.store.save_report(task.task_id, report)
        return result


class BlockingWorkflow(CompletingWorkflow):
    def __init__(self, store: StateStore, started: Event, release: Event) -> None:
        super().__init__(store)
        self.started = started
        self.release = release

    def start(self, task: TaskSpec) -> RunResult:
        state = self.store.initialize_run(task)
        self.started.set()
        assert self.release.wait(timeout=5)
        state.mark_success()
        self.store.save_state(state)
        result, report = ReportBuilder().build(task, state)
        self.store.save_result(result)
        self.store.save_report(task.task_id, report)
        return result


class FailingWorkflow:
    def start(self, task: TaskSpec) -> RunResult:
        raise RuntimeError("token=super-secret")

    def resume(self, task_id: str) -> RunResult:  # pragma: no cover - not used
        raise AssertionError(task_id)


def test_start_returns_immediately_and_final_state_is_read_from_store(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    service = TaskService(
        tmp_path,
        workflow_factory=lambda: CompletingWorkflow(store),
    )
    try:
        accepted = service.start_task("Add filtering", ["Filtering works"])
        record = service.executor.get(accepted.task_id)
        assert record is not None
        record.future.result(timeout=5)

        completed = service.get_task(accepted.task_id)
        assert accepted.status == "accepted"
        assert completed.status == "success"
        assert completed.thread_id == "thread-started"
        assert service.get_report(accepted.task_id).startswith("# 任务报告")
    finally:
        service.close(wait=True)


def test_second_task_is_rejected_while_first_is_running(tmp_path: Path) -> None:
    started = Event()
    release = Event()
    store = StateStore(tmp_path)
    service = TaskService(
        tmp_path,
        workflow_factory=lambda: BlockingWorkflow(store, started, release),
    )
    try:
        first = service.start_task("First", ["First works"])
        assert started.wait(timeout=5)

        with pytest.raises(TaskConflictError, match="already running"):
            service.start_task("Second", ["Second works"])

        release.set()
        record = service.executor.get(first.task_id)
        assert record is not None
        record.future.result(timeout=5)
    finally:
        release.set()
        service.close(wait=True)


def test_resume_uses_existing_task_and_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    task = TaskSpec(
        task_id="resume-me",
        requirement="Resume",
        acceptance_criteria=["Finishes"],
    )
    state = store.initialize_run(task)
    state.thread_id = "thread-existing"
    store.save_state(state)
    service = TaskService(
        tmp_path,
        workflow_factory=lambda: CompletingWorkflow(store),
    )
    try:
        resumed = service.resume_task(task.task_id)
        record = service.executor.get(task.task_id)
        assert record is not None
        record.future.result(timeout=5)

        assert resumed.status == "running"
        assert service.get_task(task.task_id).status == "success"
        assert service.get_task(task.task_id).thread_id == "thread-existing"
    finally:
        service.close(wait=True)


def test_background_failure_is_exposed_as_redacted_infrastructure_error(
    tmp_path: Path,
) -> None:
    service = TaskService(tmp_path, workflow_factory=FailingWorkflow)
    try:
        accepted = service.start_task("Fail safely", ["Error is reported"])
        record = service.executor.get(accepted.task_id)
        assert record is not None
        with pytest.raises(RuntimeError):
            record.future.result(timeout=5)

        snapshot = service.get_task(accepted.task_id)
        assert snapshot.status == "infrastructure_error"
        assert "super-secret" not in (snapshot.infrastructure_error or "")
    finally:
        service.close(wait=True)


def test_invalid_missing_and_not_ready_tasks_have_distinct_errors(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = TaskSpec(
        task_id="not-ready",
        requirement="Wait",
        acceptance_criteria=["Eventually completes"],
    )
    store.initialize_run(task)
    service = TaskService(tmp_path)
    try:
        with pytest.raises(InvalidTaskIdError):
            service.get_task("../outside")
        with pytest.raises(TaskNotFoundError):
            service.get_task("missing-task")
        with pytest.raises(TaskNotReadyError):
            service.get_report(task.task_id)
    finally:
        service.close(wait=True)


def test_task_detail_finds_a_subtask_in_its_queue_directory(tmp_path: Path) -> None:
    queue_store = QueueStore(tmp_path)
    spec = TaskQueueSpec.from_inputs(
        "Queue",
        [
            {"requirement": "First", "acceptance_criteria": ["Works"]},
            {"requirement": "Second", "acceptance_criteria": ["Works"]},
        ],
        queue_id="queue-detail",
    )
    spec.base_commit = "a" * 40
    queue_store.initialize_queue(spec)
    child_store = queue_store.subtask_store(spec.queue_id)
    child = spec.subtasks[0]
    state = child_store.initialize_run(child)
    state.mark_success()
    child_store.save_state(state)

    service = TaskService(tmp_path)
    try:
        snapshot = service.get_task(child.task_id)
    finally:
        service.close(wait=True)

    assert snapshot.queue_id == spec.queue_id
    assert snapshot.sequence == 1
    assert snapshot.task_id == child.task_id


def test_inactive_task_can_pause_resume_and_cancel_at_a_checkpoint(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = TaskSpec(
        task_id="controlled-task",
        requirement="Keep checkpoint",
        acceptance_criteria=["Can resume"],
    )
    state = store.initialize_run(task)
    original_phase = state.phase
    service = TaskService(
        tmp_path,
        workflow_factory=lambda: CompletingWorkflow(store),
    )
    try:
        paused = service.request_control(task.task_id, "pause")
        assert paused.status == "paused"
        assert store.load_state(task.task_id).phase is original_phase
        assert task.task_id in store.unfinished_task_ids()

        service.resume_task(task.task_id)
        record = service.executor.get(task.task_id)
        assert record is not None
        record.future.result(timeout=5)
        assert service.get_task(task.task_id).status == "success"

        cancelled_task = TaskSpec(
            task_id="cancelled-task",
            requirement="Stop safely",
            acceptance_criteria=["Stops"],
        )
        store.initialize_run(cancelled_task)
        cancelled = service.request_control(cancelled_task.task_id, "cancel")
        assert cancelled.status == "cancelled"
        assert cancelled.finished_at is not None
    finally:
        service.close(wait=True)


def test_rerun_creates_a_new_task_with_the_same_definition(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    service = TaskService(
        tmp_path,
        workflow_factory=lambda: CompletingWorkflow(store),
    )
    try:
        first = service.start_task("Repeat me", ["Still works"])
        first_record = service.executor.get(first.task_id)
        assert first_record is not None
        first_record.future.result(timeout=5)

        rerun = service.rerun_task(first.task_id)
        rerun_record = service.executor.get(rerun.task_id)
        assert rerun_record is not None
        rerun_record.future.result(timeout=5)
    finally:
        service.close(wait=True)

    assert rerun.task_id != first.task_id
    assert rerun.rerun_of == first.task_id
    assert rerun.requirement == first.requirement
    assert rerun.acceptance_criteria == first.acceptance_criteria


def test_control_request_is_durable_before_a_gated_task_initializes(
    tmp_path: Path,
) -> None:
    gate = BoundedSemaphore(1)
    gate.acquire()
    store = StateStore(tmp_path)
    executor = TaskExecutor(global_gate=gate)
    service = TaskService(
        tmp_path,
        executor=executor,
        workflow_factory=lambda: CompletingWorkflow(store),
    )
    try:
        accepted = service.start_task("Pause early", ["Does not disappear"])
        projected = service.request_control(accepted.task_id, "pause")

        assert projected.status == "pausing"
        assert service.get_task(accepted.task_id).status == "pausing"
        assert store.load_control(accepted.task_id)["action"] == "pause"
    finally:
        gate.release()
        service.close(wait=True)


def test_archive_initial_and_retry_callbacks_use_separate_checkpoints(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = TaskSpec(
        task_id="archive-callback",
        requirement="Archive safely",
        acceptance_criteria=["Retry does not rerun the archiver role"],
    )
    state = store.initialize_run(task)
    state.mark_success()
    state.delivery_status = DeliveryStatus.COMMITTED
    store.save_state(state)
    calls: list[str] = []

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(f"archive:{task_id}")
        current = store.load_state(task_id)
        current.delivery_status = DeliveryStatus.FAILED
        store.save_state(current)
        return {"status": "failed"}

    def retry(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(f"retry:{task_id}")
        current = store.load_state(task_id)
        current.delivery_status = DeliveryStatus.ARCHIVED
        store.save_state(current)
        return {"status": "completed"}

    service = TaskService(
        tmp_path,
        archive_callback=archive,
        archive_retry_callback=retry,
    )
    try:
        service._archive_after_commit(task.task_id, store)
        snapshot = service.retry_archive(task.task_id)
    finally:
        service.close(wait=True)

    assert calls == ["archive:archive-callback", "retry:archive-callback"]
    assert snapshot.delivery_status == "archived"
