from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import socket
import subprocess
from typing import Any

import pytest

from orchestrator.codex_loop.audit import AuditRecorder
from orchestrator.codex_loop.codex_client import CodexRunResult
from orchestrator.codex_loop.context import ContextSnapshot, merge_context_snapshots
from orchestrator.codex_loop.models import (
    CommandResult,
    InfrastructureError,
    PromptKind,
    RunPhase,
    RunStatus,
    TaskSpec,
    ValidationRound,
)
from orchestrator.codex_loop.state import ActiveRunError, StateStore, _atomic_write_json
from orchestrator.codex_loop.workflow import OrchestrationWorkflow
from orchestrator.codex_loop.workspace import WorkspaceManager


class FakeCodexClient:
    def __init__(
        self,
        *,
        thread_id: str = "thread-one",
        enter_error: Exception | None = None,
        run_error: Exception | None = None,
        verify_error: Exception | None = None,
        turn_missing: bool = False,
        recovered_items: list[dict[str, Any]] | None = None,
        history_complete: bool = True,
        effective_config: dict[str, Any] | None = None,
    ) -> None:
        self.saved_thread_id = thread_id
        self.enter_error = enter_error
        self.run_error = run_error
        self.verify_error = verify_error
        self.turn_missing = turn_missing
        self.recovered_items = list(recovered_items or [])
        self.history_complete = history_complete
        self.effective_config_value = effective_config
        self.start_calls = 0
        self.resume_calls: list[str] = []
        self.prompts: list[str] = []
        self.verify_calls: list[int] = []
        self.closed = False

    def __enter__(self) -> "FakeCodexClient":
        if self.enter_error:
            raise self.enter_error
        return self

    def __exit__(self, *_args: Any) -> None:
        self.closed = True

    def start_thread(self) -> str:
        self.start_calls += 1
        return self.saved_thread_id

    def resume_thread(self, thread_id: str) -> str:
        self.resume_calls.append(thread_id)
        self.saved_thread_id = thread_id
        return thread_id

    def run(self, prompt: str) -> CodexRunResult:
        self.prompts.append(prompt)
        if self.run_error:
            raise self.run_error
        return CodexRunResult(
            thread_id=self.saved_thread_id,
            final_response="done",
            turn_id=f"turn-{len(self.prompts)}",
            usage={"total_tokens": 10},
        )

    def verify_turn_completed(
        self, expected_turn_count: int
    ) -> CodexRunResult | None:
        self.verify_calls.append(expected_turn_count)
        if self.verify_error:
            raise self.verify_error
        if self.turn_missing:
            return None
        return CodexRunResult(
            thread_id=self.saved_thread_id,
            final_response="recovered",
            turn_id=f"turn-{expected_turn_count}",
            items=self.recovered_items,
            history_complete=self.history_complete,
        )

    def verify_thread_workspace(self) -> None:
        return None

    def effective_config(self) -> dict[str, Any]:
        return self.effective_config_value or {
            "approval_policy": "never",
            "default_permissions": "loop-harness",
            "sandbox_mode": None,
            "web_search": "disabled",
            "permissions": {
                "loop-harness": {
                    "extends": ":read-only",
                    "filesystem": {
                        ":root": "write",
                    },
                    "network": {"enabled": True},
                }
            },
        }


class FakeValidator:
    def __init__(
        self,
        outcomes: list[tuple[bool, list[int]]],
        *,
        preflight_error: Exception | None = None,
        validate_error: Exception | None = None,
    ) -> None:
        self.baseline = {"backend/tests/test_before.py": "digest"}
        self._protected_test_paths = set(self.baseline)
        self.outcomes = deque(outcomes)
        self.preflight_error = preflight_error
        self.validate_error = validate_error
        self.preflight_calls = 0
        self.round_numbers: list[int] = []

    @property
    def protected_test_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._protected_test_paths))

    def protect_tests(self, paths: list[str]) -> None:
        self._protected_test_paths.update(paths)

    def preflight(self) -> None:
        self.preflight_calls += 1
        if self.preflight_error:
            raise self.preflight_error

    def validate(self, round_number: int) -> ValidationRound:
        self.round_numbers.append(round_number)
        if self.validate_error:
            raise self.validate_error
        passed, exit_codes = self.outcomes.popleft()
        results = [
            CommandResult(
                command=["validation", str(index)],
                cwd="/repo",
                stage="full",
                exit_code=exit_code,
                stdout="ok" if exit_code == 0 else "",
                stderr="failure output" if exit_code else "",
            )
            for index, exit_code in enumerate(exit_codes, start=1)
        ]
        failures = [result for result in results if not result.passed]
        return ValidationRound(
            round_number=round_number,
            full_results=results,
            passed=passed,
            stage="full",
            failure_summary=(
                "; ".join(
                    f"{' '.join(item.command)}: exit code {item.exit_code}"
                    for item in failures
                )
                if failures
                else ""
            ),
        )


class EventClient:
    def __init__(self) -> None:
        self.event_sink: Any | None = None


class FakeContextAssembler:
    def __init__(self) -> None:
        self.event_sink: Any | None = None
        self.knowledge = type("Knowledge", (), {"client": EventClient()})()
        self.skills = type("Skills", (), {"client": EventClient()})()
        self.calls: list[tuple[str, str]] = []

    def assemble(
        self,
        *,
        path: str | Path,
        stage: str,
        query: str,
        actor: str,
        **_kwargs: Any,
    ) -> ContextSnapshot:
        target = Path(path)
        self.calls.append((stage, target.name))
        if target.is_file():
            snapshot = ContextSnapshot.from_dict(
                json.loads(target.read_text(encoding="utf-8"))
            )
            snapshot.verify_hash()
            return snapshot
        snapshot = merge_context_snapshots(
            stage,
            ContextSnapshot(stage=stage, query=query, actor=actor),
        )
        _atomic_write_json(target, snapshot.to_dict())
        if self.event_sink is not None:
            self.event_sink(
                "context.assembled",
                {
                    "stage": stage,
                    "snapshot_sha256": snapshot.snapshot_sha256,
                },
            )
        return snapshot


class FakeEvaluationCoordinator:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = deque(outputs)
        self.event_sink: Any | None = None
        self.context_hashes: list[str] = []

    def evaluate(
        self,
        *,
        context: ContextSnapshot,
        validation_round: int,
        artifact_root: str | Path,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.context_hashes.append(context.snapshot_sha256)
        aggregate = {
            "schema_version": 1,
            "validation_round": validation_round,
            "syntax": {"status": "pass"},
            "logic": {"status": "pass"},
            "specification": {"status": "pass"},
            "architecture": {"status": "not_evaluated"},
            "requires_repair": False,
            "blocking_findings": [],
            "warnings": [],
            "information": [],
            "context_sha256": context.snapshot_sha256,
            **self.outputs.popleft(),
        }
        root = Path(artifact_root)
        round_root = root / f"round-{validation_round:02d}"
        _atomic_write_json(round_root / "spec.json", aggregate["specification"])
        _atomic_write_json(
            round_root / "architecture.json", aggregate["architecture"]
        )
        _atomic_write_json(round_root / "aggregate.json", aggregate)
        _atomic_write_json(root / "spec.json", aggregate["specification"])
        _atomic_write_json(root / "architecture.json", aggregate["architecture"])
        _atomic_write_json(root / "aggregate.json", aggregate)
        if self.event_sink is not None:
            self.event_sink(
                "evaluation.completed",
                {
                    "validation_round": validation_round,
                    "requires_repair": aggregate["requires_repair"],
                    "context_sha256": context.snapshot_sha256,
                },
            )
        return aggregate


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "backend/tests").mkdir(parents=True)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend/tests/test_before.py").write_text(
        "def test_before(): pass\n", encoding="utf-8"
    )
    (tmp_path / "frontend/package.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        ".codex-orchestrator/\n.codex-runtime/\nnode_modules/\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return tmp_path


def task() -> TaskSpec:
    return TaskSpec(
        task_id="workflow-test",
        requirement="实现一个功能",
        acceptance_criteria=["行为符合验收标准"],
    )


def workflow_with(
    repo: Path, client: FakeCodexClient, validator: FakeValidator
) -> OrchestrationWorkflow:
    return OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: client,
        validator_factory=lambda _root, _baseline: validator,
    )


def initialize_isolated_run(repo: Path, saved_task: TaskSpec):
    store = StateStore(repo)
    workspace = WorkspaceManager(repo).create(saved_task)
    state = store.initialize_run(
        saved_task,
        task_repo_root=workspace.worktree,
        workspace={
            "base_ref": workspace.base_ref,
            "base_commit": workspace.base_commit,
            "task_branch": workspace.task_branch,
            "worktree_relative_path": workspace.worktree_relative_path,
            "source_worktree_was_dirty": workspace.source_worktree_was_dirty,
        },
    )
    store.save_manifest(saved_task.task_id, workspace.manifest())
    return store, state


def checkpoint_saved_turn(
    store: StateStore,
    state: Any,
    *,
    prompt: str = "saved exact prompt",
) -> AuditRecorder:
    audit = AuditRecorder(
        store.run_dir(state.task_id), state.repo_root, state.base_commit
    )
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.phase = RunPhase.CODEX_TURN
    state.pending_prompt_kind = None
    state.last_diff_sha256 = audit.current_diff_sha256()
    prompt_path = audit.save_prompt(1, prompt)
    audit.append(
        "turn.started",
        {
            "prompt_kind": "initial",
            "prompt_path": prompt_path.relative_to(audit.run_dir).as_posix(),
            "diff_sha256": state.last_diff_sha256,
            "changed_paths": audit.changed_paths(),
        },
        source="codex",
        turn_number=1,
        redacted=True,
    )
    store.save_state(state)
    return audit


def test_first_turn_success_creates_thread_state_logs_and_report(repo: Path) -> None:
    client = FakeCodexClient()
    validator = FakeValidator([(True, [0, 0, 0])])

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.SUCCESS
    assert result.thread_id == "thread-one"
    assert result.turn_count == 1
    assert result.failure_count == 0
    assert client.start_calls == 1
    assert client.resume_calls == []
    assert len(client.prompts) == 1
    assert "实现一个功能" in client.prompts[0]
    run_dir = repo / ".codex-orchestrator/runs/workflow-test"
    assert (run_dir / "state.json").is_file()
    assert (run_dir / "result.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert len(list((run_dir / "logs/round-01").glob("*.log"))) == 3
    saved_state = StateStore(repo).load_state("workflow-test")
    assert saved_state.protected_test_paths == ["backend/tests/test_before.py"]
    assert not (repo / ".codex-orchestrator/active.lock").exists()


def test_frozen_context_and_four_layer_result_flow_through_workflow(
    repo: Path,
) -> None:
    client = FakeCodexClient()
    validator = FakeValidator([(True, [0, 0, 0])])
    contexts = FakeContextAssembler()
    evaluator = FakeEvaluationCoordinator([{}])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: client,
        validator_factory=lambda _root, _baseline: validator,
        context_assembler=contexts,  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.start(task())

    assert result.status is RunStatus.SUCCESS
    run_dir = repo / ".codex-orchestrator/runs/workflow-test"
    generation = ContextSnapshot.from_dict(
        json.loads(
            (run_dir / "context/generation.json").read_text(encoding="utf-8")
        )
    )
    evaluation = ContextSnapshot.from_dict(
        json.loads(
            (run_dir / "context/evaluation.json").read_text(encoding="utf-8")
        )
    )
    generation.verify_hash()
    evaluation.verify_hash()
    assert generation.snapshot_sha256 in client.prompts[0]
    assert evaluator.context_hashes == [evaluation.snapshot_sha256]
    assert [stage for stage, _name in contexts.calls] == [
        "generation",
        "spec_evaluation",
        "architecture_evaluation",
    ]
    aggregate = json.loads(
        (run_dir / "evaluations/aggregate.json").read_text(encoding="utf-8")
    )
    assert aggregate["syntax"]["status"] == "pass"
    assert aggregate["logic"]["status"] == "pass"
    assert aggregate["architecture"]["status"] == "not_evaluated"


def test_blocking_evaluation_reuses_generator_thread_and_frozen_context(
    repo: Path,
) -> None:
    client = FakeCodexClient()
    validator = FakeValidator(
        [(True, [0, 0, 0]), (True, [0, 0, 0])]
    )
    contexts = FakeContextAssembler()
    evaluator = FakeEvaluationCoordinator(
        [
            {
                "specification": {"status": "fail"},
                "requires_repair": True,
                "blocking_findings": [
                    {
                        "layer": "specification",
                        "acceptance_id": "AC-001",
                        "evidence": "backend/tests/test_before.py",
                    }
                ],
            },
            {},
        ]
    )
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: client,
        validator_factory=lambda _root, _baseline: validator,
        context_assembler=contexts,  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.start(task())

    assert result.status is RunStatus.SUCCESS
    assert result.failure_count == 1
    assert result.turn_count == 2
    assert client.start_calls == 1
    assert client.resume_calls == []
    assert "# 独立评估发现" in client.prompts[1]
    generation = ContextSnapshot.from_dict(
        json.loads(
            (
                repo
                / ".codex-orchestrator/runs/workflow-test/context/generation.json"
            ).read_text(encoding="utf-8")
        )
    )
    assert all(
        generation.snapshot_sha256 in prompt for prompt in client.prompts
    )
    assert len(evaluator.context_hashes) == 2
    assert len(set(evaluator.context_hashes)) == 1
    assert [stage for stage, _name in contexts.calls].count("generation") == 1


def test_effective_permissions_are_verified_before_prompt(repo: Path) -> None:
    client = FakeCodexClient(
        effective_config={
            "approval_policy": "never",
            "default_permissions": "loop-harness",
            "sandbox_mode": None,
            "web_search": "disabled",
            "permissions": {
                "loop-harness": {
                    "extends": ":read-only",
                    "filesystem": {
                        ":root": "read",
                    },
                    "network": {"enabled": True},
                }
            },
        }
    )
    validator = FakeValidator([(True, [0])])

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert client.prompts == []
    assert "external-sandbox profile is unknown" in (
        result.infrastructure_error or ""
    )


def test_one_failure_then_success_uses_one_repair_on_same_thread(repo: Path) -> None:
    client = FakeCodexClient()
    validator = FakeValidator([(False, [1]), (True, [0, 0, 0])])

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.SUCCESS
    assert result.failure_count == 1
    assert result.turn_count == 2
    assert result.thread_id == "thread-one"
    assert client.start_calls == 1
    assert len(client.prompts) == 2
    assert "# 上一轮验证失败" in client.prompts[1]
    assert "failure output" in client.prompts[1]


def test_multiple_failed_commands_in_one_round_increment_only_once(repo: Path) -> None:
    client = FakeCodexClient()
    validator = FakeValidator([(False, [1, 2, 0]), (True, [0, 0, 0])])

    result = workflow_with(repo, client, validator).start(task())

    assert len(result.rounds[0].failed_results) == 2
    assert result.failure_count == 1
    assert result.turn_count == 2
    assert result.status is RunStatus.SUCCESS


def test_third_failure_stops_after_initial_and_two_repairs(repo: Path) -> None:
    client = FakeCodexClient()
    validator = FakeValidator(
        [(False, [1]), (False, [1]), (False, [1])]
    )

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.MANUAL_REVIEW
    assert result.failure_count == 3
    assert result.turn_count == 3
    assert validator.round_numbers == [1, 2, 3]
    assert len(client.prompts) == 3
    assert sum("# 同一任务的第" in prompt for prompt in client.prompts) == 2


def test_resume_uses_saved_thread_and_pending_repair(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    state.thread_id = "saved-thread"
    state.turn_count = 1
    failed = FakeValidator([(False, [1])]).validate(1)
    state.add_round(failed)
    assert state.phase is RunPhase.PROMPT_PENDING
    assert state.pending_prompt_kind is PromptKind.REPAIR
    store.save_state(state)

    client = FakeCodexClient(thread_id="unused")
    validator = FakeValidator([(True, [0, 0, 0])])
    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    assert result.thread_id == "saved-thread"
    assert client.start_calls == 0
    assert client.resume_calls == ["saved-thread"]
    assert len(client.prompts) == 1
    assert "# 上一轮验证失败" in client.prompts[0]


def test_resume_rehydrates_legacy_redacted_paths_from_manifest(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.phase = RunPhase.VALIDATION_PENDING
    state.pending_prompt_kind = None
    store.save_state(state)

    state_path = store.run_dir(saved_task.task_id) / "state.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted["repo_root"] = "[REDACTED]/.codex-orchestrator/worktrees/workflow-test"
    persisted["control_repo_root"] = "[REDACTED]"
    state_path.write_text(
        json.dumps(persisted, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    client = FakeCodexClient()
    validator = FakeValidator([(True, [0, 0, 0])])
    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    assert validator.round_numbers == [1]


def test_resume_from_in_progress_turn_does_not_duplicate_prompt(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.phase = RunPhase.CODEX_TURN
    state.pending_prompt_kind = None
    store.save_state(state)

    client = FakeCodexClient()
    validator = FakeValidator([(True, [0, 0, 0])])
    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    assert client.resume_calls == ["saved-thread"]
    assert client.prompts == []
    assert client.verify_calls == [1]
    assert result.turn_count == 1


def test_resume_dispatches_saved_prompt_once_when_app_server_has_no_turn(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    audit = checkpoint_saved_turn(store, state)
    client = FakeCodexClient(turn_missing=True)
    validator = FakeValidator([(True, [0, 0, 0])])

    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    assert client.prompts == ["saved exact prompt"]
    events = [
        json.loads(line)
        for line in audit.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(event["type"] == "prompt.saved" for event in events) == 1
    assert sum(event["type"] == "turn.started" for event in events) == 1
    assert sum(event["type"] == "turn.completed" for event in events) == 1


def test_resume_backfills_completed_items_without_duplicate_events(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    audit = checkpoint_saved_turn(store, state)
    worktree = Path(state.repo_root)
    (worktree / "declared.txt").write_text("from Codex\n", encoding="utf-8")
    recovered_items = [
        {
            "id": "file-1",
            "type": "fileChange",
            "status": "completed",
            "changes": [
                {"path": "declared.txt", "diff": "from Codex\n", "kind": {"type": "add"}}
            ],
        },
        {
            "id": "command-1",
            "type": "commandExecution",
            "command": "python -m pytest",
            "status": "completed",
            "exitCode": 0,
            "aggregatedOutput": "passed",
        },
    ]
    client = FakeCodexClient(recovered_items=recovered_items)
    validator = FakeValidator([(True, [0, 0, 0])])

    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    events = [
        json.loads(line)
        for line in audit.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(event["type"] == "file.changed" for event in events) == 1
    assert sum(event["type"] == "command.completed" for event in events) == 1
    assert sum(event["type"] == "codex.item.completed" for event in events) == 2


def test_durable_pause_retains_checkpoint_and_resume_continues_it(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    store.request_control(saved_task.task_id, "pause")
    first_client = FakeCodexClient()

    paused = workflow_with(
        repo,
        first_client,
        FakeValidator([(True, [0])]),
    ).resume(saved_task.task_id)

    checkpoint = store.load_state(saved_task.task_id)
    assert paused.status is RunStatus.PAUSED
    assert checkpoint.status is RunStatus.PAUSED
    assert checkpoint.phase is RunPhase.PROMPT_PENDING
    assert checkpoint.pending_prompt_kind is PromptKind.INITIAL
    assert store.load_control(saved_task.task_id) is None
    assert first_client.prompts == []

    resumed_client = FakeCodexClient(thread_id="unused")
    resumed = workflow_with(
        repo,
        resumed_client,
        FakeValidator([(True, [0])]),
    ).resume(saved_task.task_id)

    assert resumed.status is RunStatus.SUCCESS
    assert len(resumed_client.prompts) == 1
    events = [
        json.loads(line)
        for line in (store.run_dir(saved_task.task_id) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(event["type"] == "run.paused" for event in events)
    assert any(event["type"] == "run.resumed" for event in events)


def test_durable_cancel_finishes_without_running_the_pending_prompt(repo: Path) -> None:
    saved_task = task()
    store, _state = initialize_isolated_run(repo, saved_task)
    store.request_control(saved_task.task_id, "cancel")
    client = FakeCodexClient()

    cancelled = workflow_with(
        repo,
        client,
        FakeValidator([(True, [0])]),
    ).resume(saved_task.task_id)

    assert cancelled.status is RunStatus.CANCELLED
    assert client.prompts == []
    assert store.load_state(saved_task.task_id).phase is RunPhase.COMPLETED


def test_resume_rejects_worktree_change_not_declared_by_recovered_turn(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    checkpoint_saved_turn(store, state)
    (Path(state.repo_root) / "manual.txt").write_text(
        "external change\n", encoding="utf-8"
    )
    client = FakeCodexClient(recovered_items=[])
    validator = FakeValidator([(True, [0, 0, 0])])

    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert "not declared" in (result.infrastructure_error or "")
    assert validator.round_numbers == []


def test_resume_rejects_incomplete_turn_history(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    checkpoint_saved_turn(store, state)
    client = FakeCodexClient(history_complete=False)
    validator = FakeValidator([(True, [0, 0, 0])])

    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert "history is incomplete" in (result.infrastructure_error or "")
    assert validator.round_numbers == []


def test_resume_finishes_saved_passing_validation_without_rerunning_it(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    audit = AuditRecorder(store.run_dir(saved_task.task_id), state.repo_root, state.base_commit)
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.phase = RunPhase.VALIDATING
    audit.append("validation.started", {}, round_number=1)
    passed = ValidationRound(
        round_number=1,
        full_results=[],
        passed=True,
        stage="full",
    )
    store.save_round(saved_task.task_id, passed)
    state.add_round(passed)
    state.phase = RunPhase.VALIDATING
    state.last_diff_sha256 = audit.current_diff_sha256()
    store.save_state(state)
    client = FakeCodexClient()
    validator = FakeValidator([])

    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    assert validator.round_numbers == []
    assert audit.has_event("validation.completed", round_number=1)


def test_resume_rebuilds_missing_final_artifacts(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    audit = AuditRecorder(store.run_dir(saved_task.task_id), state.repo_root, state.base_commit)
    (Path(state.repo_root) / "finished.txt").write_text("done\n", encoding="utf-8")
    state.last_diff_sha256 = audit.current_diff_sha256()
    state.mark_success("?? finished.txt")
    store.save_state(state)

    result = workflow_with(
        repo, FakeCodexClient(), FakeValidator([])
    ).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    run_dir = store.run_dir(saved_task.task_id)
    assert (run_dir / "result.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "changes/files.json").is_file()
    assert (run_dir / "changes/final.diff").is_file()


def test_resume_refuses_validation_when_saved_turn_cannot_be_confirmed(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.phase = RunPhase.CODEX_TURN
    state.pending_prompt_kind = None
    store.save_state(state)

    client = FakeCodexClient(
        verify_error=InfrastructureError("saved turn is interrupted")
    )
    validator = FakeValidator([(True, [0, 0, 0])])
    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert result.failure_count == 0
    assert result.turn_count == 1
    assert validator.round_numbers == []
    assert client.prompts == []


def test_unfinished_state_blocks_a_different_new_task_without_an_active_process(
    repo: Path,
) -> None:
    store = StateStore(repo)
    first = task()
    store.initialize_run(first)
    second = TaskSpec(
        task_id="second-task",
        requirement="另一个功能",
        acceptance_criteria=["另一个标准"],
    )
    client = FakeCodexClient()
    validator = FakeValidator([(True, [0, 0, 0])])

    with pytest.raises(ActiveRunError, match="unfinished task.*workflow-test"):
        workflow_with(repo, client, validator).start(second)

    assert not (repo / ".codex-orchestrator/active.lock").exists()
    assert not (store.run_dir(second.task_id) / "state.json").exists()


def test_stale_lock_allows_a_to_resume_then_b_can_start_after_a_finishes(
    repo: Path,
) -> None:
    store, _state = initialize_isolated_run(repo, task())
    store.root.mkdir(parents=True, exist_ok=True)
    store.active_lock_path.write_text(
        json.dumps(
            {
                "task_id": "workflow-test",
                "pid": 999_999_999,
                "token": "stale-token",
                "hostname": socket.gethostname(),
                "acquired_at": "2026-07-16T00:00:00+08:00",
            }
        ),
        encoding="utf-8",
    )
    second = TaskSpec(
        task_id="second-task",
        requirement="另一个功能",
        acceptance_criteria=["另一个标准"],
    )

    with pytest.raises(ActiveRunError, match="unfinished task.*workflow-test"):
        workflow_with(
            repo, FakeCodexClient(), FakeValidator([(True, [0])])
        ).start(second)

    first_result = workflow_with(
        repo, FakeCodexClient(), FakeValidator([(True, [0])])
    ).resume("workflow-test")
    second_result = workflow_with(
        repo, FakeCodexClient(), FakeValidator([(True, [0])])
    ).start(second)

    assert first_result.status is RunStatus.SUCCESS
    assert second_result.status is RunStatus.SUCCESS
    assert not store.active_lock_path.exists()


def test_unexpected_orchestrator_exception_becomes_reported_infrastructure_error(
    repo: Path,
) -> None:
    class BrokenPromptRenderer:
        def initial_prompt(self, *_args: Any) -> str:
            raise OSError("template unavailable")

    client = FakeCodexClient()
    validator = FakeValidator([(True, [0, 0, 0])])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: client,
        validator_factory=lambda _root, _baseline: validator,
        prompt_renderer=BrokenPromptRenderer(),  # type: ignore[arg-type]
    )

    result = workflow.start(task())

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert result.failure_count == 0
    assert "OSError" in (result.infrastructure_error or "")
    assert (repo / ".codex-orchestrator/runs/workflow-test/report.md").is_file()
    assert not (repo / ".codex-orchestrator/active.lock").exists()


def test_validation_infrastructure_error_persists_partial_command_logs(
    repo: Path,
) -> None:
    class PartialInfrastructureValidator(FakeValidator):
        def __init__(self) -> None:
            super().__init__([])

        def validate(self, round_number: int) -> ValidationRound:
            self.round_numbers.append(round_number)
            completed = CommandResult(
                command=["first-check"],
                cwd=str(repo),
                stage="full",
                exit_code=0,
                stdout="completed before failure",
            )
            failed_to_start = CommandResult(
                command=["second-check"],
                cwd=str(repo),
                stage="full",
                exit_code=None,
                stderr="npm could not start",
                infrastructure_error="npm could not start",
            )
            return ValidationRound(
                round_number=round_number,
                full_results=[completed, failed_to_start],
                passed=False,
                stage="full",
                failure_summary="second-check: infrastructure error",
                infrastructure_error="npm could not start",
            )

    client = FakeCodexClient()
    validator = PartialInfrastructureValidator()

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert result.failure_count == 0
    assert len(result.rounds) == 1
    assert len(result.rounds[0].full_results) == 2
    log_dir = repo / ".codex-orchestrator/runs/workflow-test/logs/round-01"
    assert len(list(log_dir.glob("*.log"))) == 2


@pytest.mark.parametrize(
    "failure_location",
    ["validation_preflight", "sdk_preflight", "turn", "validation"],
)
def test_infrastructure_error_stops_without_counting_validation_failure(
    repo: Path, failure_location: str
) -> None:
    error = InfrastructureError("network or local tool unavailable")
    client = FakeCodexClient(
        enter_error=error if failure_location == "sdk_preflight" else None,
        run_error=error if failure_location == "turn" else None,
    )
    validator = FakeValidator(
        [(True, [0])],
        preflight_error=(
            error if failure_location == "validation_preflight" else None
        ),
        validate_error=error if failure_location == "validation" else None,
    )
    if failure_location in {"validation_preflight", "sdk_preflight"}:
        expected_turns = 0
    else:
        expected_turns = 1

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert result.failure_count == 0
    assert result.turn_count == expected_turns
    assert result.infrastructure_error == "network or local tool unavailable"
    assert not (repo / ".codex-orchestrator/active.lock").exists()
