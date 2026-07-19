from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from orchestrator.codex_loop.audit import AuditRecorder
from orchestrator.codex_loop.models import (
    DeliveryStatus,
    InfrastructureError,
    PromptKind,
    QueueStatus,
    QueueTaskStatus,
    ReviewStatus,
    RunPhase,
    RunResult,
    RunStatus,
    TaskQueueSpec,
    TaskSpec,
)
from orchestrator.codex_loop.queue_workflow import QueueWorkflow
from orchestrator.codex_loop.state import QueueStore, StateStore
from orchestrator.codex_loop.workspace import WorkspaceManager


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / ".gitignore").write_text(".codex-orchestrator/\n")
    (tmp_path / "tracked.txt").write_text("baseline\n")
    git(tmp_path, "add", ".")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "baseline",
    )
    return tmp_path


def subtask(requirement: str) -> dict[str, object]:
    return {
        "requirement": requirement,
        "acceptance_criteria": [f"{requirement} works"],
    }


class CompletingWorkflow:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def start(self, task: TaskSpec) -> RunResult:
        state = self.store.initialize_run(task)
        state.thread_id = f"thread-{task.sequence}"
        state.mark_success()
        self.store.save_state(state)
        result = RunResult.from_run(task, state)
        self.store.save_result(result)
        return result

    def resume(self, task_id: str) -> RunResult:
        return self.store.load_result(task_id)


def workflow_factory(
    store: StateStore,
    _base_commit: str,
    _inherited_path: Path | None,
    _inherited_sha: str,
) -> CompletingWorkflow:
    return CompletingWorkflow(store)


def test_queue_definition_requires_two_ordered_subtasks() -> None:
    with pytest.raises(ValueError, match="at least two"):
        TaskQueueSpec.from_inputs("One", [subtask("only")])

    queue = TaskQueueSpec.from_inputs(
        "Ordered",
        [subtask("first"), subtask("second")],
        queue_id="queue-test",
    )
    assert [task.sequence for task in queue.subtasks] == [1, 2]
    assert [task.task_id for task in queue.subtasks] == [
        "queue-test-task-01",
        "queue-test-task-02",
    ]
    assert TaskQueueSpec.from_dict(queue.to_dict()) == queue

    queue.subtasks[1].sequence = 1
    with pytest.raises(ValueError, match="continuous and ordered"):
        TaskQueueSpec(
            queue_id=queue.queue_id,
            name=queue.name,
            subtasks=queue.subtasks,
        )


def test_queue_stores_subtasks_below_parent_and_waits_for_review(
    repository: Path,
) -> None:
    workflow = QueueWorkflow(repository, workflow_factory=workflow_factory)
    prepared = workflow.prepare(
        "Two steps",
        [subtask("first"), subtask("second")],
        queue_id="queue-store",
    )

    after_machine = workflow.run_current(prepared.queue_id)

    assert after_machine.status is QueueStatus.WAITING_REVIEW
    assert after_machine.current_task_id == "queue-store-task-01"
    assert after_machine.subtasks[0].status is QueueTaskStatus.WAITING_REVIEW
    assert after_machine.subtasks[1].status is QueueTaskStatus.PENDING
    queue_dir = QueueStore(repository).queue_dir("queue-store")
    assert (queue_dir / "queue.json").is_file()
    assert (queue_dir / "subtasks/queue-store-task-01/task.json").is_file()
    assert not (repository / ".codex-orchestrator/runs/queue-store-task-01").exists()

    unchanged = workflow.run_current(prepared.queue_id)
    assert unchanged.subtasks[1].status is QueueTaskStatus.PENDING


def test_approved_cumulative_diff_becomes_next_worktree_index_baseline(
    repository: Path,
) -> None:
    queue = TaskQueueSpec.from_inputs(
        "Inherited",
        [subtask("first"), subtask("second")],
        queue_id="queue-inherit",
    )
    queue.base_commit = git(repository, "rev-parse", "HEAD")
    first = queue.subtasks[0]
    first_workspace = WorkspaceManager(
        repository, base_ref=queue.base_commit
    ).create(first)
    (first_workspace.worktree / "tracked.txt").write_text("first\n")
    (first_workspace.worktree / "binary.bin").write_bytes(b"\x00\x01\xff")
    first_run = repository / "first-run"
    first_audit = AuditRecorder(
        first_run,
        first_workspace.worktree,
        queue.base_commit,
        queue_task=True,
    )
    changes = first_audit.capture_final_changes()
    cumulative = changes["cumulative_diff"]
    cumulative_path = first_run / str(cumulative["path"])

    second = queue.subtasks[1]
    manager = WorkspaceManager(repository, base_ref=queue.base_commit)
    second_workspace = manager.create(second)
    manager.apply_inherited_diff(
        second_workspace,
        cumulative_path,
        str(cumulative["stored_sha256"]),
    )

    assert (second_workspace.worktree / "tracked.txt").read_text() == "first\n"
    assert (second_workspace.worktree / "binary.bin").read_bytes() == b"\x00\x01\xff"
    assert git(second_workspace.worktree, "diff", "--cached", "--name-only").splitlines() == [
        "binary.bin",
        "tracked.txt",
    ]
    second_audit = AuditRecorder(
        repository / "second-run",
        second_workspace.worktree,
        queue.base_commit,
        inherited_baseline=True,
        queue_task=True,
    )
    assert second_audit.changed_paths() == []
    (second_workspace.worktree / "tracked.txt").write_text("second\n")
    assert second_audit.changed_paths() == ["tracked.txt"]
    assert git(repository, "show", "main:tracked.txt") == "baseline"

    third_workspace = manager.create(
        TaskSpec(
            task_id="queue-inherit-task-03",
            requirement="third",
            acceptance_criteria=["works"],
            queue_id="queue-inherit",
            sequence=3,
        )
    )
    with pytest.raises(InfrastructureError, match="SHA-256"):
        manager.apply_inherited_diff(
            third_workspace,
            cumulative_path,
            "0" * 64,
        )


def prepare_real_review(
    repository: Path,
) -> tuple[QueueWorkflow, QueueStore, StateStore, str, str]:
    workflow = QueueWorkflow(repository, workflow_factory=workflow_factory)
    prepared = workflow.prepare(
        "Review history",
        [subtask("first"), subtask("second")],
        queue_id="queue-review",
    )
    queue_store = QueueStore(repository)
    spec = queue_store.load_spec(prepared.queue_id)
    task = spec.subtasks[0]
    manager = WorkspaceManager(repository, base_ref=spec.base_commit)
    workspace = manager.create(task)
    store = queue_store.subtask_store(prepared.queue_id)
    state = store.initialize_run(
        task,
        task_repo_root=workspace.worktree,
        workspace={
            "base_ref": workspace.base_ref,
            "base_commit": workspace.base_commit,
            "task_branch": workspace.task_branch,
            "worktree_relative_path": workspace.worktree_relative_path,
        },
    )
    state.thread_id = "thread-review"
    store.save_manifest(task.task_id, workspace.manifest())
    (workspace.worktree / "tracked.txt").write_text("first revision\n")
    audit = AuditRecorder(
        store.run_dir(task.task_id),
        workspace.worktree,
        workspace.base_commit,
        queue_task=True,
    )
    changes = audit.capture_final_changes()
    state.last_diff_sha256 = str(changes["final_diff"]["raw_sha256"])
    state.mark_success()
    store.save_state(state)
    store.save_result(RunResult.from_run(task, state))

    queue_state = queue_store.load_state(prepared.queue_id)
    child = queue_state.subtasks[0]
    child.status = QueueTaskStatus.WAITING_REVIEW
    child.machine_status = RunStatus.SUCCESS
    child.thread_id = state.thread_id
    queue_state.current_task_id = task.task_id
    queue_state.status = QueueStatus.WAITING_REVIEW
    queue_store.save_state(queue_state)
    return workflow, queue_store, store, task.task_id, state.last_diff_sha256


def test_changes_requested_reuses_child_and_preserves_review_history(
    repository: Path,
) -> None:
    workflow, queue_store, store, task_id, first_sha = prepare_real_review(repository)

    queue_state, first_review = workflow.record_review(
        "queue-review",
        task_id,
        decision="changes_requested",
        reviewer="Reviewer",
        comment="Please revise the implementation.",
        reviewed_diff_sha256=first_sha,
    )

    reopened = store.load_state(task_id)
    assert first_review.review_number == 1
    assert queue_state.current_task_id == task_id
    assert queue_state.status is QueueStatus.RUNNING
    assert reopened.status is RunStatus.RUNNING
    assert reopened.pending_prompt_kind is PromptKind.REVIEW_REPAIR
    assert reopened.thread_id == "thread-review"
    assert (store.run_dir(task_id) / "reviews/review-01.json").is_file()
    assert (
        store.run_dir(task_id) / "changes/revisions/revision-01.diff"
    ).is_file()

    task = store.load_task(task_id)
    (Path(reopened.repo_root) / "tracked.txt").write_text("second revision\n")
    audit = AuditRecorder(
        store.run_dir(task_id),
        reopened.repo_root,
        reopened.base_commit,
        queue_task=True,
    )
    changes = audit.capture_final_changes()
    reopened.last_diff_sha256 = str(changes["final_diff"]["raw_sha256"])
    reopened.review_status = ReviewStatus.PENDING
    reopened.mark_success()
    store.save_state(reopened)
    store.save_result(RunResult.from_run(task, reopened))
    queue_state = queue_store.load_state("queue-review")
    child = queue_state.task(task_id)
    child.status = QueueTaskStatus.WAITING_REVIEW
    child.machine_status = RunStatus.SUCCESS
    child.review_status = ReviewStatus.PENDING
    queue_state.status = QueueStatus.WAITING_REVIEW
    queue_store.save_state(queue_state)

    approved_state, second_review = workflow.record_review(
        "queue-review",
        task_id,
        decision="approved",
        reviewer="Reviewer",
        comment="Approved.",
        reviewed_diff_sha256=reopened.last_diff_sha256,
    )

    assert second_review.review_number == 2
    assert approved_state.status is QueueStatus.PENDING
    assert approved_state.current_task_id is None
    assert approved_state.subtasks[0].status is QueueTaskStatus.COMPLETED
    assert approved_state.subtasks[1].status is QueueTaskStatus.PENDING
    assert approved_state.cumulative_diff_sha256
    assert (store.run_dir(task_id) / "reviews/review-02.json").is_file()

    cumulative_path = queue_store.cumulative_diff_path("queue-review")
    cumulative_path.write_text("tampered\n", encoding="utf-8")

    def hash_checking_factory(
        child_store: StateStore,
        _base_commit: str,
        inherited_path: Path | None,
        inherited_sha: str,
    ) -> CompletingWorkflow:
        if inherited_path is not None:
            actual = hashlib.sha256(inherited_path.read_bytes()).hexdigest()
            if actual != inherited_sha:
                raise InfrastructureError("Approved cumulative diff SHA-256 changed")
        return CompletingWorkflow(child_store)

    stopped = QueueWorkflow(
        repository,
        queue_store=queue_store,
        workflow_factory=hash_checking_factory,
    ).run_current("queue-review")
    assert stopped.status is QueueStatus.INFRASTRUCTURE_ERROR
    assert stopped.current_task_id == "queue-review-task-02"


def test_rejected_review_stops_the_queue(repository: Path) -> None:
    workflow, queue_store, _store, task_id, diff_sha = prepare_real_review(repository)

    rejected, review = workflow.record_review(
        "queue-review",
        task_id,
        decision="rejected",
        reviewer="Reviewer",
        comment="Do not continue.",
        reviewed_diff_sha256=diff_sha,
    )

    assert review.decision is ReviewStatus.REJECTED
    assert rejected.status is QueueStatus.REJECTED
    assert rejected.subtasks[0].status is QueueTaskStatus.REJECTED
    assert rejected.subtasks[1].status is QueueTaskStatus.PENDING
    assert workflow.run_current("queue-review").status is QueueStatus.REJECTED


def test_approved_queue_delivery_retries_commit_then_archives_before_advancing(
    repository: Path,
) -> None:
    _workflow, queue_store, store, task_id, diff_sha = prepare_real_review(
        repository
    )

    class FlakyDelivery:
        def __init__(self) -> None:
            self.calls = 0

        def deliver(self, selected_task_id: str, **_kwargs: object) -> dict[str, str]:
            self.calls += 1
            run_state = store.load_state(selected_task_id)
            if self.calls == 1:
                run_state.delivery_status = DeliveryStatus.FAILED
                store.save_state(run_state)
                raise InfrastructureError("injected commit interruption")
            run_state.delivery_status = DeliveryStatus.COMMITTED
            store.save_state(run_state)
            return {"status": "committed", "commit_sha": "a" * 40}

    delivery = FlakyDelivery()
    archive_calls: list[str] = []

    def archive(selected_task_id: str, *, store: StateStore) -> dict[str, str]:
        archive_calls.append(selected_task_id)
        run_state = store.load_state(selected_task_id)
        assert run_state.delivery_status is DeliveryStatus.COMMITTED
        run_state.delivery_status = DeliveryStatus.ARCHIVED
        store.save_state(run_state)
        return {"status": "completed"}

    recovering = QueueWorkflow(
        repository,
        queue_store=queue_store,
        workflow_factory=workflow_factory,
        delivery_factory=lambda _store: delivery,  # type: ignore[arg-type]
        archive_callback=archive,
    )

    with pytest.raises(InfrastructureError, match="injected commit interruption"):
        recovering.record_review(
            "queue-review",
            task_id,
            decision="approved",
            reviewer="Reviewer",
            comment="Approved.",
            reviewed_diff_sha256=diff_sha,
        )

    failed = queue_store.load_state("queue-review")
    assert failed.status is QueueStatus.INFRASTRUCTURE_ERROR
    assert failed.task(task_id).review_status is ReviewStatus.APPROVED
    assert archive_calls == []

    resumed = recovering.resume("queue-review")

    assert delivery.calls == 2
    assert archive_calls == [task_id]
    assert resumed.status is QueueStatus.WAITING_REVIEW
    assert resumed.current_task_id == "queue-review-task-02"
    assert resumed.task(task_id).status is QueueTaskStatus.COMPLETED
    assert resumed.task(task_id).delivery_status is DeliveryStatus.ARCHIVED
    assert resumed.cumulative_diff_sha256


def test_infrastructure_error_resumes_only_the_current_subtask(
    repository: Path,
) -> None:
    attempts = {"count": 0}

    class FailsOnceWorkflow(CompletingWorkflow):
        def start(self, task: TaskSpec) -> RunResult:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("runtime unavailable")
            return super().start(task)

    def factory(
        store: StateStore,
        _base_commit: str,
        _inherited_path: Path | None,
        _inherited_sha: str,
    ) -> FailsOnceWorkflow:
        return FailsOnceWorkflow(store)

    workflow = QueueWorkflow(repository, workflow_factory=factory)
    prepared = workflow.prepare(
        "Retry",
        [subtask("first"), subtask("second")],
        queue_id="queue-retry",
    )

    failed = workflow.run_current(prepared.queue_id)
    resumed = workflow.resume(prepared.queue_id)

    assert failed.status is QueueStatus.INFRASTRUCTURE_ERROR
    assert failed.current_task_id == "queue-retry-task-01"
    assert resumed.status is QueueStatus.WAITING_REVIEW
    assert resumed.current_task_id == "queue-retry-task-01"
    assert resumed.subtasks[1].status is QueueTaskStatus.PENDING


def test_incomplete_codex_turn_restarts_its_recorded_prompt_before_validation(
    repository: Path,
) -> None:
    observed_checkpoints: list[tuple[RunPhase, PromptKind | None]] = []

    class IncompleteTurnWorkflow(CompletingWorkflow):
        def start(self, task: TaskSpec) -> RunResult:
            workspace = WorkspaceManager(repository).create(task)
            state = self.store.initialize_run(
                task,
                task_repo_root=workspace.worktree,
                workspace={
                    "base_ref": workspace.base_ref,
                    "base_commit": workspace.base_commit,
                    "task_branch": workspace.task_branch,
                    "worktree_relative_path": workspace.worktree_relative_path,
                },
            )
            self.store.save_manifest(task.task_id, workspace.manifest())
            state.thread_id = "saved-thread"
            state.turn_count = 1
            state.cycle_turn_count = 1
            state.phase = RunPhase.CODEX_TURN
            state.pending_prompt_kind = None
            audit = AuditRecorder(
                self.store.run_dir(task.task_id),
                state.repo_root,
                state.base_commit,
                queue_task=True,
            )
            prompt_path = audit.save_prompt(1, "old initial prompt")
            audit.append(
                "turn.started",
                {
                    "prompt_kind": PromptKind.INITIAL.value,
                    "prompt_path": prompt_path.relative_to(audit.run_dir).as_posix(),
                    "diff_sha256": audit.current_diff_sha256(),
                    "changed_paths": [],
                },
                source="codex",
                turn_number=1,
            )
            state.mark_infrastructure_error("turn interrupted")
            self.store.save_state(state)
            result = RunResult.from_run(task, state)
            self.store.save_result(result)
            return result

        def resume(self, task_id: str) -> RunResult:
            task = self.store.load_task(task_id)
            state = self.store.load_state(task_id)
            observed_checkpoints.append((state.phase, state.pending_prompt_kind))
            state.mark_success()
            self.store.save_state(state)
            result = RunResult.from_run(task, state)
            self.store.save_result(result)
            return result

    def factory(
        store: StateStore,
        _base_commit: str,
        _inherited_path: Path | None,
        _inherited_sha: str,
    ) -> IncompleteTurnWorkflow:
        return IncompleteTurnWorkflow(store)

    workflow = QueueWorkflow(repository, workflow_factory=factory)
    prepared = workflow.prepare(
        "Retry incomplete turn",
        [subtask("first"), subtask("second")],
        queue_id="queue-incomplete-turn",
    )

    failed = workflow.run_current(prepared.queue_id)
    resumed = workflow.resume(prepared.queue_id)

    assert failed.status is QueueStatus.INFRASTRUCTURE_ERROR
    assert resumed.status is QueueStatus.WAITING_REVIEW
    assert observed_checkpoints == [(RunPhase.PROMPT_PENDING, PromptKind.INITIAL)]


def test_paused_child_releases_queue_and_resumes_the_same_checkpoint(
    repository: Path,
) -> None:
    starts = {"count": 0}

    class PausesOnceWorkflow(CompletingWorkflow):
        def start(self, task: TaskSpec) -> RunResult:
            starts["count"] += 1
            state = self.store.initialize_run(task)
            state.thread_id = "thread-paused"
            state.mark_paused()
            self.store.save_state(state)
            result = RunResult.from_run(task, state)
            self.store.save_result(result)
            return result

        def resume(self, task_id: str) -> RunResult:
            task = self.store.load_task(task_id)
            state = self.store.load_state(task_id)
            state.reopen_after_pause()
            state.mark_success()
            self.store.save_state(state)
            result = RunResult.from_run(task, state)
            self.store.save_result(result)
            return result

    def factory(
        store: StateStore,
        _base_commit: str,
        _inherited_path: Path | None,
        _inherited_sha: str,
    ) -> PausesOnceWorkflow:
        return PausesOnceWorkflow(store)

    workflow = QueueWorkflow(repository, workflow_factory=factory)
    prepared = workflow.prepare(
        "Pause queue",
        [subtask("first"), subtask("second")],
        queue_id="queue-paused",
    )

    paused = workflow.run_current(prepared.queue_id)
    resumed = workflow.resume(prepared.queue_id)

    assert paused.status is QueueStatus.PAUSED
    assert paused.subtasks[0].status is QueueTaskStatus.PAUSED
    assert resumed.status is QueueStatus.WAITING_REVIEW
    assert resumed.current_task_id == "queue-paused-task-01"
    assert resumed.subtasks[0].status is QueueTaskStatus.WAITING_REVIEW
    assert starts["count"] == 1
