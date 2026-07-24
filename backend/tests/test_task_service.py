from __future__ import annotations

from pathlib import Path
from threading import BoundedSemaphore, Event
from unittest.mock import Mock

import pytest

from backend.exceptions.business_exception import (
    InvalidTaskIdError,
    TaskConflictError,
    TaskNotFoundError,
    TaskNotReadyError,
)
from backend.service.task_service import TaskService
from backend.utils.task_executor import TaskExecutor
from codex_loop.models import (
    DeliveryStatus,
    ReviewRecord,
    ReviewStatus,
    RunResult,
    TaskQueueSpec,
    TaskSpec,
)
from codex_loop.report import ReportBuilder
from codex_loop.state import QueueStore, StateStore


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


def _review_delivery_doubles(
    store: StateStore,
    calls: list[str],
) -> tuple[Mock, Mock]:
    def record(
        task_id: str,
        *,
        decision: str,
        reviewer: str,
        comment: str,
        reviewed_diff_sha256: str,
        commit_subject: str,
    ) -> ReviewRecord:
        calls.append("review")
        state = store.load_state(task_id)
        review = ReviewRecord(
            task_id=task_id,
            decision=ReviewStatus(decision),
            reviewer=reviewer,
            comment=comment,
            machine_status=state.status,
            reviewed_diff_sha256=reviewed_diff_sha256,
            commit_subject=commit_subject,
        )
        store.save_review(review)
        state.review_status = review.decision
        state.delivery_status = DeliveryStatus.COMMIT_PENDING
        store.save_state(state)
        return review

    def deliver(
        task_id: str,
        *,
        review: ReviewRecord,
    ) -> dict[str, object]:
        assert review.decision is ReviewStatus.APPROVED
        calls.append("commit")
        state = store.load_state(task_id)
        state.delivery_status = DeliveryStatus.COMMITTED
        store.save_state(state)
        return {"status": "committed"}

    review_service = Mock()
    review_service.record.side_effect = record
    delivery_service = Mock()
    delivery_service.deliver.side_effect = deliver
    return review_service, delivery_service


def _successful_task(store: StateStore, task_id: str) -> TaskSpec:
    task = TaskSpec(
        task_id=task_id,
        requirement="Archive after approval",
        acceptance_criteria=["Approval returns before archive completes"],
    )
    state = store.initialize_run(task)
    state.mark_success()
    store.save_state(state)
    return task


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
    retry_started = Event()
    retry_release = Event()

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(f"archive:{task_id}")
        current = store.load_state(task_id)
        current.delivery_status = DeliveryStatus.FAILED
        store.save_state(current)
        return {"status": "failed"}

    def retry(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(f"retry:{task_id}")
        retry_started.set()
        assert retry_release.wait(timeout=5)
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
        archive_dir = store.run_dir(task.task_id) / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "outbox.json").write_text("{}", encoding="utf-8")
        snapshot = service.retry_archive(task.task_id)
        duplicate = service.retry_archive(task.task_id)

        assert snapshot.delivery_status == "archive_pending"
        assert duplicate.delivery_status == "archive_pending"
        assert retry_started.wait(timeout=5)
        assert calls == ["archive:archive-callback", "retry:archive-callback"]

        record = service.executor.get(task.task_id)
        assert record is not None
        retry_release.set()
        record.future.result(timeout=5)
        completed = service.get_task(task.task_id)
    finally:
        retry_release.set()
        service.close(wait=True)

    assert calls == ["archive:archive-callback", "retry:archive-callback"]
    assert completed.delivery_status == "archived"


def test_standalone_archive_retry_without_outbox_restarts_initial_archiver(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "archive-retry-before-outbox")
    state = store.load_state(task.task_id)
    state.review_status = ReviewStatus.APPROVED
    state.delivery_status = DeliveryStatus.FAILED
    store.save_state(state)
    calls: list[str] = []
    archive_started = Event()
    archive_release = Event()

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(f"archive:{task_id}")
        assert not (store.run_dir(task_id) / "archive" / "outbox.json").exists()
        archive_started.set()
        assert archive_release.wait(timeout=5)
        current = store.load_state(task_id)
        current.delivery_status = DeliveryStatus.ARCHIVED
        store.save_state(current)
        return {"status": "completed"}

    def retry(task_id: str, *, store: StateStore) -> dict[str, object]:
        del store
        calls.append(f"retry:{task_id}")
        raise AssertionError("retry callback requires an existing outbox")

    service = TaskService(
        tmp_path,
        archive_callback=archive,
        archive_retry_callback=retry,
    )
    try:
        snapshot = service.retry_archive(task.task_id)

        assert snapshot.delivery_status == "archive_pending"
        assert archive_started.wait(timeout=5)
        assert calls == ["archive:archive-retry-before-outbox"]

        record = service.executor.get(task.task_id)
        assert record is not None
        archive_release.set()
        record.future.result(timeout=5)
        completed = service.get_task(task.task_id)
    finally:
        archive_release.set()
        service.close(wait=True)

    assert calls == ["archive:archive-retry-before-outbox"]
    assert completed.delivery_status == "archived"


def test_standalone_approval_returns_while_archive_runs_in_background(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "archive-background")
    calls: list[str] = []
    review_service, delivery_service = _review_delivery_doubles(store, calls)
    archive_started = Event()
    archive_release = Event()

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append("archive")
        archive_started.set()
        assert archive_release.wait(timeout=5)
        state = store.load_state(task_id)
        state.delivery_status = DeliveryStatus.ARCHIVED
        store.save_state(state)
        return {"status": "completed"}

    service = TaskService(
        tmp_path,
        review_service=review_service,
        delivery_service=delivery_service,
        archive_callback=archive,
    )
    try:
        snapshot = service.review_task(
            task.task_id,
            decision="approved",
            reviewer="Reviewer",
            comment="Looks good",
            reviewed_diff_sha256="a" * 64,
            commit_subject="archive asynchronously",
        )

        assert snapshot.review_status == "approved"
        assert snapshot.delivery_status == "archive_pending"
        assert service.get_task(task.task_id).delivery_status == "archive_pending"
        assert calls[:2] == ["review", "commit"]
        assert archive_started.wait(timeout=5)
        record = service.executor.get(task.task_id)
        assert record is not None
        assert not record.future.done()

        archive_release.set()
        record.future.result(timeout=5)
        assert service.get_task(task.task_id).delivery_status == "archived"
    finally:
        archive_release.set()
        service.close(wait=True)


def test_standalone_approval_without_archive_callback_remains_committed(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "commit-without-archive")
    calls: list[str] = []
    review_service, delivery_service = _review_delivery_doubles(store, calls)
    service = TaskService(
        tmp_path,
        review_service=review_service,
        delivery_service=delivery_service,
    )
    try:
        snapshot = service.review_task(
            task.task_id,
            decision="approved",
            reviewer="Reviewer",
            comment="Looks good",
            reviewed_diff_sha256="d" * 64,
            commit_subject="commit without archive",
        )
    finally:
        service.close(wait=True)

    assert snapshot.review_status == "approved"
    assert snapshot.delivery_status == "committed"
    assert calls == ["review", "commit"]


def test_standalone_background_archive_failure_is_persisted_and_redacted(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "archive-background-failure")
    calls: list[str] = []
    review_service, delivery_service = _review_delivery_doubles(store, calls)
    archive_started = Event()
    archive_release = Event()

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        del task_id, store
        archive_started.set()
        assert archive_release.wait(timeout=5)
        raise RuntimeError("token=archive-super-secret")

    service = TaskService(
        tmp_path,
        review_service=review_service,
        delivery_service=delivery_service,
        archive_callback=archive,
    )
    try:
        snapshot = service.review_task(
            task.task_id,
            decision="approved",
            reviewer="Reviewer",
            comment="Looks good",
            reviewed_diff_sha256="b" * 64,
            commit_subject="archive asynchronously",
        )
        assert snapshot.delivery_status == "archive_pending"
        assert archive_started.wait(timeout=5)

        archive_release.set()
        record = service.executor.get(task.task_id)
        assert record is not None
        record.future.result(timeout=5)

        failed = service.get_task(task.task_id)
        assert failed.review_status == "approved"
        assert failed.delivery_status == "failed"
        assert "archive-super-secret" not in failed.last_error_summary
        events = (
            store.run_dir(task.task_id) / "events.jsonl"
        ).read_text(encoding="utf-8")
        assert "knowledge.write_failed" in events
        assert "archive-super-secret" not in events
    finally:
        archive_release.set()
        service.close(wait=True)


def test_standalone_archive_waits_for_an_existing_executor_operation(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "archive-after-busy")
    calls: list[str] = []
    review_service, delivery_service = _review_delivery_doubles(store, calls)
    busy_started = Event()
    busy_release = Event()
    archive_started = Event()
    archive_release = Event()
    executor = TaskExecutor()

    def busy_operation() -> None:
        busy_started.set()
        assert busy_release.wait(timeout=5)

    executor.submit_operation("busy-operation", busy_operation)
    assert busy_started.wait(timeout=5)

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        archive_started.set()
        assert archive_release.wait(timeout=5)
        state = store.load_state(task_id)
        state.delivery_status = DeliveryStatus.ARCHIVED
        store.save_state(state)
        return {"status": "completed"}

    service = TaskService(
        tmp_path,
        executor=executor,
        review_service=review_service,
        delivery_service=delivery_service,
        archive_callback=archive,
    )
    try:
        snapshot = service.review_task(
            task.task_id,
            decision="approved",
            reviewer="Reviewer",
            comment="Looks good",
            reviewed_diff_sha256="c" * 64,
            commit_subject="archive after busy work",
        )
        assert snapshot.delivery_status == "archive_pending"
        assert not archive_started.is_set()

        busy_release.set()
        assert archive_started.wait(timeout=5)
        record = service.executor.get(task.task_id)
        assert record is not None
        archive_release.set()
        record.future.result(timeout=5)
        assert service.get_task(task.task_id).delivery_status == "archived"
    finally:
        busy_release.set()
        archive_release.set()
        service.close(wait=True)


def test_get_task_recovers_commit_to_archive_gap_after_restart(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "archive-restart")
    state = store.load_state(task.task_id)
    state.review_status = ReviewStatus.APPROVED
    state.delivery_status = DeliveryStatus.COMMITTED
    store.save_state(state)
    calls: list[str] = []
    archive_started = Event()
    archive_release = Event()

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(task_id)
        archive_started.set()
        assert archive_release.wait(timeout=5)
        current = store.load_state(task_id)
        current.delivery_status = DeliveryStatus.ARCHIVED
        store.save_state(current)
        return {"status": "completed"}

    service = TaskService(tmp_path, archive_callback=archive)
    try:
        first = service.get_task(task.task_id)
        second = service.get_task(task.task_id)

        assert first.delivery_status == "archive_pending"
        assert second.delivery_status == "archive_pending"
        assert archive_started.wait(timeout=5)
        assert calls == [task.task_id]

        record = service.executor.get(task.task_id)
        assert record is not None
        archive_release.set()
        record.future.result(timeout=5)
        assert service.get_task(task.task_id).delivery_status == "archived"
    finally:
        archive_release.set()
        service.close(wait=True)


def test_get_task_retries_a_persisted_outbox_after_restart(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "archive-outbox-restart")
    state = store.load_state(task.task_id)
    state.review_status = ReviewStatus.APPROVED
    state.delivery_status = DeliveryStatus.ARCHIVE_PENDING
    store.save_state(state)
    archive_dir = store.run_dir(task.task_id) / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "outbox.json").write_text("{}", encoding="utf-8")
    calls: list[str] = []
    retry_started = Event()
    retry_release = Event()

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        del store
        calls.append(f"archive:{task_id}")
        raise AssertionError("an existing outbox must skip the initial archiver")

    def retry(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(f"retry:{task_id}")
        retry_started.set()
        assert retry_release.wait(timeout=5)
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
        first = service.get_task(task.task_id)
        second = service.get_task(task.task_id)

        assert first.delivery_status == "archive_pending"
        assert second.delivery_status == "archive_pending"
        assert retry_started.wait(timeout=5)
        assert calls == ["retry:archive-outbox-restart"]

        record = service.executor.get(task.task_id)
        assert record is not None
        retry_release.set()
        record.future.result(timeout=5)
        assert service.get_task(task.task_id).delivery_status == "archived"
    finally:
        retry_release.set()
        service.close(wait=True)


@pytest.mark.parametrize(
    "callback_status",
    [DeliveryStatus.ARCHIVE_PENDING, DeliveryStatus.COMMITTED],
)
def test_archive_callback_that_leaves_nonterminal_state_is_marked_failed(
    tmp_path: Path,
    callback_status: DeliveryStatus,
) -> None:
    store = StateStore(tmp_path)
    task = _successful_task(store, "archive-no-terminal-state")
    calls: list[str] = []
    review_service, delivery_service = _review_delivery_doubles(store, calls)
    archive_started = Event()
    archive_release = Event()

    def archive(task_id: str, *, store: StateStore) -> dict[str, object]:
        archive_started.set()
        assert archive_release.wait(timeout=5)
        state = store.load_state(task_id)
        state.delivery_status = callback_status
        store.save_state(state)
        return {"status": "completed"}

    service = TaskService(
        tmp_path,
        review_service=review_service,
        delivery_service=delivery_service,
        archive_callback=archive,
    )
    try:
        snapshot = service.review_task(
            task.task_id,
            decision="approved",
            reviewer="Reviewer",
            comment="Looks good",
            reviewed_diff_sha256="e" * 64,
            commit_subject="require terminal archive state",
        )
        assert snapshot.delivery_status == "archive_pending"
        assert archive_started.wait(timeout=5)

        record = service.executor.get(task.task_id)
        assert record is not None
        archive_release.set()
        record.future.result(timeout=5)

        failed = service.get_task(task.task_id)
        assert failed.delivery_status == "failed"
        assert (
            failed.last_error_summary
            == "archive callback completed without a terminal delivery status"
        )
    finally:
        archive_release.set()
        service.close(wait=True)


def test_queue_archive_retry_keeps_its_existing_synchronous_behavior(
    tmp_path: Path,
) -> None:
    queue_store = QueueStore(tmp_path)
    spec = TaskQueueSpec.from_inputs(
        "Queue archive retry",
        [
            {
                "requirement": "Retry queued archive",
                "acceptance_criteria": ["Queue behavior is unchanged"],
            },
            {
                "requirement": "Remain pending",
                "acceptance_criteria": ["Second task is not modified"],
            },
        ],
        queue_id="queue-archive-retry",
    )
    spec.base_commit = "a" * 40
    queue_store.initialize_queue(spec)
    task = spec.subtasks[0]
    child_store = queue_store.subtask_store(spec.queue_id)
    state = child_store.initialize_run(task)
    state.mark_success()
    state.review_status = ReviewStatus.APPROVED
    state.delivery_status = DeliveryStatus.FAILED
    child_store.save_state(state)
    calls: list[str] = []

    def retry(task_id: str, *, store: StateStore) -> dict[str, object]:
        calls.append(task_id)
        current = store.load_state(task_id)
        current.delivery_status = DeliveryStatus.ARCHIVED
        store.save_state(current)
        return {"status": "completed"}

    service = TaskService(tmp_path, archive_retry_callback=retry)
    try:
        snapshot = service.retry_archive(task.task_id)
    finally:
        service.close(wait=True)

    assert calls == [task.task_id]
    assert snapshot.delivery_status == "archived"
    assert service.executor.get(task.task_id) is None
