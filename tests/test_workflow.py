from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import socket
import subprocess
from typing import Any

import pytest

from codex_loop.audit import AuditRecorder
from codex_loop.codex_client import CodexRunResult
from codex_loop.context import ContextSnapshot, merge_context_snapshots
from codex_loop.evaluation import (
    ArchitectureEvaluationOutput,
    EvaluationCoordinator,
    EvaluationEvidence,
    SpecCriterionResult,
    SpecEvaluationOutput,
    freeze_aggregate,
)
from codex_loop.models import (
    CommandResult,
    InfrastructureError,
    PromptKind,
    RunPhase,
    RunStatus,
    TaskSpec,
    ValidationRound,
)
from codex_loop.state import ActiveRunError, StateStore, _atomic_write_json
from codex_loop.workflow import OrchestrationWorkflow
from codex_loop.workspace import WorkspaceManager
from codex_loop.validation_evidence import ValidationEvidenceSnapshot


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

    def verify_turn_completed(self, expected_turn_count: int) -> CodexRunResult | None:
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
        task: TaskSpec,
        context: ContextSnapshot,
        changed_files: list[dict[str, Any]],
        diff_text: str,
        validation_evidence: Any,
        validation_evidence_path: str,
        artifact_root: str | Path,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.context_hashes.append(context.snapshot_sha256)
        validation_round = validation_evidence.validation_round
        binding, _normalized = EvaluationCoordinator.input_binding(
            task=task,
            context=context,
            changed_files=changed_files,
            diff_text=diff_text,
            validation_evidence=validation_evidence,
        )
        evidence_ids = [item.evidence_id for item in validation_evidence.commands]
        requested = self.outputs.popleft()
        requested_status = str(requested.get("specification", {}).get("status", "pass"))
        criteria: list[SpecCriterionResult] = []
        for index, _criterion in enumerate(task.acceptance_criteria, start=1):
            evidence = []
            validation_ids = evidence_ids if requested_status == "pass" else []
            if requested_status == "fail":
                evidence = [
                    EvaluationEvidence(
                        kind="diff",
                        source="changes/final.diff",
                        detail="Fixture-requested specification failure.",
                    )
                ]
            criteria.append(
                SpecCriterionResult(
                    acceptance_id=f"AC-{index:03d}",
                    status=requested_status,
                    rationale=f"Fixture-requested {requested_status} result.",
                    validation_evidence_ids=validation_ids,
                    evidence=evidence,
                )
            )
        spec = SpecEvaluationOutput(
            criteria=criteria,
            summary=f"Fixture specification status: {requested_status}.",
        )
        architecture = ArchitectureEvaluationOutput(
            status="not_evaluated",
            findings=[],
            summary="No applicable frozen architecture knowledge was available.",
        )
        aggregate = freeze_aggregate(
            EvaluationCoordinator._aggregate(
                spec=spec,
                architecture=architecture,
                context=context,
                validation_evidence=validation_evidence,
                validation_evidence_path=validation_evidence_path,
                binding=binding,
            )
        )
        root = Path(artifact_root)
        round_root = root / f"round-{validation_round:02d}"
        common = {
            "schema_version": 2,
            **binding,
            "validation_evidence_path": validation_evidence_path,
        }
        _atomic_write_json(
            round_root / "spec.json",
            {**common, "output": spec.model_dump(mode="json")},
        )
        _atomic_write_json(
            round_root / "architecture.json",
            {**common, "output": architecture.model_dump(mode="json")},
        )
        _atomic_write_json(round_root / "aggregate.json", aggregate)
        _atomic_write_json(
            root / "spec.json", {**common, "output": spec.model_dump(mode="json")}
        )
        _atomic_write_json(
            root / "architecture.json",
            {**common, "output": architecture.model_dump(mode="json")},
        )
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


def initialize_isolated_run(
    repo: Path,
    saved_task: TaskSpec,
    *,
    evidence_required: bool = True,
):
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
    manifest = workspace.manifest()
    if evidence_required:
        manifest["validation_evidence"] = {
            "required": True,
            "schema_version": 1,
        }
    store.save_manifest(saved_task.task_id, manifest)
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
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["validation_evidence"] == {
        "required": True,
        "schema_version": 1,
    }
    evidence = StateStore(repo).load_validation_evidence("workflow-test", 1)
    assert evidence.status == "pass"
    assert [item.evidence_id for item in evidence.commands] == [
        "VAL-001",
        "VAL-002",
        "VAL-003",
    ]
    assert result.artifacts["validation_evidence"] == (
        "validation/evidence-round-01.json"
    )
    assert result.validation_evidence_sha256 == evidence.snapshot_sha256
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    event_types = [item["type"] for item in events]
    assert event_types.index("validation.evidence.frozen") < event_types.index(
        "validation.completed"
    )
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
        json.loads((run_dir / "context/generation.json").read_text(encoding="utf-8"))
    )
    evaluation = ContextSnapshot.from_dict(
        json.loads((run_dir / "context/evaluation.json").read_text(encoding="utf-8"))
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
    assert result.artifacts == {
        **{
            "manifest": "manifest.json",
            "permissions": "permissions.json",
            "events": "events.jsonl",
            "files": "changes/files.json",
            "diff": "changes/final.diff",
            "report": "report.md",
        },
        "validation_evidence": "validation/evidence-round-01.json",
        "spec_evaluation": "evaluations/round-01/spec.json",
        "architecture_evaluation": "evaluations/round-01/architecture.json",
        "evaluation_aggregate": "evaluations/round-01/aggregate.json",
    }
    for key in (
        "validation_evidence_sha256",
        "final_diff_sha256",
        "context_sha256",
        "changed_files_sha256",
        "evaluation_input_sha256",
    ):
        assert getattr(result, key) == aggregate[key]


def test_blocking_evaluation_reuses_generator_thread_and_frozen_context(
    repo: Path,
) -> None:
    client = FakeCodexClient()
    validator = FakeValidator([(True, [0, 0, 0]), (True, [0, 0, 0])])
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
                repo / ".codex-orchestrator/runs/workflow-test/context/generation.json"
            ).read_text(encoding="utf-8")
        )
    )
    assert all(generation.snapshot_sha256 in prompt for prompt in client.prompts)
    assert len(evaluator.context_hashes) == 2
    assert len(set(evaluator.context_hashes)) == 1
    assert [stage for stage, _name in contexts.calls].count("generation") == 1


def test_evaluation_repair_then_latest_validation_failure_projects_fresh_aggregate(
    repo: Path,
) -> None:
    evaluator = FakeEvaluationCoordinator([{"specification": {"status": "fail"}}])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator(
            [(True, [0]), (False, [1]), (False, [1])]
        ),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.start(task())

    assert result.status is RunStatus.MANUAL_REVIEW
    assert result.infrastructure_error is None
    assert [item.passed for item in result.rounds] == [True, False, False]
    assert len(evaluator.context_hashes) == 1
    run_dir = StateStore(repo).run_dir("workflow-test")
    first = json.loads(
        (run_dir / "evaluations/round-01/aggregate.json").read_text(encoding="utf-8")
    )
    latest = json.loads(
        (run_dir / "evaluations/aggregate.json").read_text(encoding="utf-8")
    )
    assert first["validation_round"] == 1
    assert first["validation"]["status"] == "pass"
    assert latest["validation_round"] == 3
    assert latest["validation"]["status"] == "fail"
    assert latest["evaluation_input_sha256"] == ""
    assert not (run_dir / "evaluations/spec.json").exists()
    assert not (run_dir / "evaluations/architecture.json").exists()
    assert result.artifacts["evaluation_aggregate"] == (
        "evaluations/round-03/aggregate.json"
    )


def test_requires_human_evaluation_cannot_be_promoted_to_machine_success(
    repo: Path,
) -> None:
    evaluator = FakeEvaluationCoordinator(
        [
            {
                "specification": {"status": "needs_human"},
                "requires_human": True,
                "warnings": [
                    {
                        "layer": "specification",
                        "acceptance_id": "AC-001",
                    }
                ],
            }
        ]
    )
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([(True, [0])]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.start(task())

    assert result.status is RunStatus.MANUAL_REVIEW
    assert result.to_dict()["validation"]["passed"] is True
    assert len(evaluator.context_hashes) == 1


def test_failed_validation_rounds_never_call_evaluator(repo: Path) -> None:
    evaluator = FakeEvaluationCoordinator([])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator(
            [(False, [1]), (False, [1]), (False, [1])]
        ),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.start(task())

    assert result.status is RunStatus.MANUAL_REVIEW
    assert evaluator.context_hashes == []
    store = StateStore(repo)
    assert [
        store.load_validation_evidence("workflow-test", index).status
        for index in (1, 2, 3)
    ] == ["fail", "fail", "fail"]


def test_validation_rejects_a_diff_changed_by_fixed_commands(repo: Path) -> None:
    evaluator = FakeEvaluationCoordinator([])

    class WorkspaceMutatingValidator(FakeValidator):
        def __init__(self, root: Path) -> None:
            super().__init__([])
            self.root = root

        def validate(self, round_number: int) -> ValidationRound:
            self.round_numbers.append(round_number)
            (self.root / "changed-during-validation.txt").write_text(
                "unexpected\n",
                encoding="utf-8",
            )
            return ValidationRound(
                round_number=round_number,
                full_results=[
                    CommandResult(
                        command=["fixed-check"],
                        cwd=str(self.root),
                        stage="full",
                        exit_code=0,
                        stdout="passed before mutation check",
                    )
                ],
                passed=True,
                stage="full",
            )

    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda root, _baseline: WorkspaceMutatingValidator(root),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.start(task())

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert "changed while fixed validation" in (result.infrastructure_error or "")
    assert evaluator.context_hashes == []
    evidence = StateStore(repo).load_validation_evidence("workflow-test", 1)
    assert evidence.status == "infra_error"


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
    assert "external-sandbox profile is unknown" in (result.infrastructure_error or "")


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


def test_repair_diff_does_not_invalidate_the_previous_round_evidence(
    repo: Path,
) -> None:
    class RepairEditingClient(FakeCodexClient):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root

        def run(self, prompt: str) -> CodexRunResult:
            result = super().run(prompt)
            if len(self.prompts) == 2:
                (self.root / "repair.txt").write_text(
                    "repaired\n",
                    encoding="utf-8",
                )
            return result

    clients: list[RepairEditingClient] = []

    def client_factory(root: Path) -> RepairEditingClient:
        client = RepairEditingClient(root)
        clients.append(client)
        return client

    workflow = OrchestrationWorkflow(
        repo,
        client_factory=client_factory,
        validator_factory=lambda _root, _baseline: FakeValidator(
            [(False, [1]), (True, [0])]
        ),
    )

    result = workflow.start(task())

    assert result.status is RunStatus.SUCCESS
    store = StateStore(repo)
    first = store.load_validation_evidence("workflow-test", 1)
    second = store.load_validation_evidence("workflow-test", 2)
    assert first.final_diff_sha256 != second.final_diff_sha256
    assert len(clients[0].prompts) == 2


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
    validator = FakeValidator([(False, [1]), (False, [1]), (False, [1])])

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.MANUAL_REVIEW
    assert result.failure_count == 3
    assert result.turn_count == 3
    assert validator.round_numbers == [1, 2, 3]
    assert len(client.prompts) == 3
    assert sum("# 同一任务的第" in prompt for prompt in client.prompts) == 2


def test_resume_uses_saved_thread_and_pending_repair(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(
        repo,
        saved_task,
        evidence_required=False,
    )
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
                {
                    "path": "declared.txt",
                    "diff": "from Codex\n",
                    "kind": {"type": "add"},
                }
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
    audit = AuditRecorder(
        store.run_dir(saved_task.task_id), state.repo_root, state.base_commit
    )
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.phase = RunPhase.VALIDATING
    state.active_validation_round = 1
    state.validation_start_diff_sha256 = audit.current_diff_sha256()
    state.last_diff_sha256 = state.validation_start_diff_sha256
    audit.append(
        "validation.started",
        {"diff_sha256": state.validation_start_diff_sha256},
        round_number=1,
    )
    command = CommandResult(
        command=["validation", "saved"],
        cwd=state.repo_root,
        stage="full",
        exit_code=0,
        stdout="saved pass",
    )
    store.write_command_log(saved_task.task_id, 1, 1, command)
    passed = ValidationRound(
        round_number=1,
        full_results=[command],
        passed=True,
        stage="full",
    )
    store.save_round(saved_task.task_id, passed)
    evidence = ValidationEvidenceSnapshot.from_round(
        passed,
        task_id=saved_task.task_id,
        base_commit=state.base_commit,
        final_diff_sha256=state.last_diff_sha256,
        control_repo_root=repo,
        run_dir=store.run_dir(saved_task.task_id),
    )
    store.save_validation_evidence(saved_task.task_id, evidence)
    store.save_state(state)
    client = FakeCodexClient()
    validator = FakeValidator([])

    result = workflow_with(repo, client, validator).resume(saved_task.task_id)

    assert result.status is RunStatus.SUCCESS
    assert validator.round_numbers == []
    assert audit.has_event("validation.completed", round_number=1)


def test_resume_recovers_diff_change_frozen_during_validation(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    audit = AuditRecorder(
        store.run_dir(saved_task.task_id), state.repo_root, state.base_commit
    )
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.cycle_turn_count = 1
    state.phase = RunPhase.VALIDATING
    state.active_validation_round = 1
    state.validation_start_diff_sha256 = audit.current_diff_sha256()
    state.last_diff_sha256 = state.validation_start_diff_sha256
    audit.append(
        "validation.started",
        {"diff_sha256": state.validation_start_diff_sha256},
        round_number=1,
    )
    (Path(state.repo_root) / "changed-during-validation.txt").write_text(
        "unexpected\n",
        encoding="utf-8",
    )
    final_diff_sha256 = audit.current_diff_sha256()
    assert final_diff_sha256 != state.validation_start_diff_sha256
    command = CommandResult(
        command=["fixed-check"],
        cwd=state.repo_root,
        stage="full",
        exit_code=0,
        stdout="passed before mutation check",
    )
    store.write_command_log(saved_task.task_id, 1, 1, command)
    message = "Task worktree changed while fixed validation commands were running"
    frozen_round = ValidationRound(
        round_number=1,
        full_results=[command],
        passed=False,
        stage="full",
        failure_summary=message,
        infrastructure_error=message,
    )
    store.save_round(saved_task.task_id, frozen_round)
    evidence = ValidationEvidenceSnapshot.from_round(
        frozen_round,
        task_id=saved_task.task_id,
        base_commit=state.base_commit,
        final_diff_sha256=final_diff_sha256,
        control_repo_root=repo,
        run_dir=store.run_dir(saved_task.task_id),
    )
    store.save_validation_evidence(saved_task.task_id, evidence)
    store.save_state(state)
    validator = FakeValidator([])
    evaluator = FakeEvaluationCoordinator([])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: validator,
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.resume(saved_task.task_id)

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert result.infrastructure_error == message
    assert validator.round_numbers == []
    assert evaluator.context_hashes == []
    assert len(result.rounds) == 1
    assert result.rounds[0].infrastructure_error == message
    aggregate = json.loads(
        (
            store.run_dir(saved_task.task_id) / "evaluations/round-01/aggregate.json"
        ).read_text(encoding="utf-8")
    )
    assert aggregate["validation_round"] == 1
    assert aggregate["validation"]["status"] == "infra_error"


def test_resume_promotes_command_only_infrastructure_checkpoint(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    audit = AuditRecorder(
        store.run_dir(saved_task.task_id), state.repo_root, state.base_commit
    )
    state.thread_id = "saved-thread"
    state.turn_count = 1
    state.phase = RunPhase.VALIDATING
    state.active_validation_round = 1
    state.validation_start_diff_sha256 = audit.current_diff_sha256()
    state.last_diff_sha256 = state.validation_start_diff_sha256
    audit.append(
        "validation.started",
        {"diff_sha256": state.validation_start_diff_sha256},
        round_number=1,
    )
    command = CommandResult(
        command=["fixed-check"],
        cwd=state.repo_root,
        stage="full",
        exit_code=None,
        infrastructure_error="runner unavailable",
    )
    store.write_command_log(saved_task.task_id, 1, 1, command)
    frozen_round = ValidationRound(
        round_number=1,
        full_results=[command],
        passed=False,
        stage="full",
        failure_summary="command did not start",
    )
    store.save_round(saved_task.task_id, frozen_round)
    evidence = ValidationEvidenceSnapshot.from_round(
        frozen_round,
        task_id=saved_task.task_id,
        base_commit=state.base_commit,
        final_diff_sha256=state.last_diff_sha256,
        control_repo_root=repo,
        run_dir=store.run_dir(saved_task.task_id),
    )
    assert evidence.status == "infra_error"
    assert evidence.round_infrastructure_error is None
    store.save_validation_evidence(saved_task.task_id, evidence)
    store.save_state(state)
    validator = FakeValidator([])

    result = workflow_with(repo, FakeCodexClient(), validator).resume(
        saved_task.task_id
    )

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert result.infrastructure_error == "runner unavailable"
    assert result.failure_count == 0
    assert validator.round_numbers == []


def test_resume_rejects_required_round_when_evidence_snapshot_is_missing(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(repo, saved_task)
    audit = AuditRecorder(
        store.run_dir(saved_task.task_id), state.repo_root, state.base_commit
    )
    state.thread_id = "saved-thread"
    state.turn_count = 1
    command = CommandResult(
        command=["validation", "saved"],
        cwd=state.repo_root,
        stage="full",
        exit_code=0,
        stdout="saved pass",
    )
    store.write_command_log(saved_task.task_id, 1, 1, command)
    passed = ValidationRound(
        round_number=1,
        full_results=[command],
        passed=True,
        stage="full",
    )
    store.save_round(saved_task.task_id, passed)
    state.add_round(passed)
    state.phase = RunPhase.EVALUATION_PENDING
    state.last_diff_sha256 = audit.current_diff_sha256()
    store.save_state(state)
    old_round_root = store.run_dir(saved_task.task_id) / "evaluations/round-01"
    _atomic_write_json(
        old_round_root / "aggregate.json",
        {"schema_version": 1, "requires_repair": False},
    )
    _atomic_write_json(
        old_round_root / "spec.json",
        {"schema_version": 1, "output": {"legacy": True}},
    )
    _atomic_write_json(
        old_round_root / "architecture.json",
        {"schema_version": 1, "output": {"legacy": True}},
    )
    evaluator = FakeEvaluationCoordinator([])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.resume(saved_task.task_id)

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert "required validation evidence checkpoint is missing" in (
        result.infrastructure_error or ""
    )
    assert evaluator.context_hashes == []


def test_legacy_evaluation_checkpoint_goes_to_human_without_model_call(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(
        repo,
        saved_task,
        evidence_required=False,
    )
    audit = AuditRecorder(
        store.run_dir(saved_task.task_id), state.repo_root, state.base_commit
    )
    state.thread_id = "saved-thread"
    state.turn_count = 1
    passed = ValidationRound(
        round_number=1,
        full_results=[],
        passed=True,
        stage="full",
    )
    store.save_round(saved_task.task_id, passed)
    state.add_round(passed)
    state.phase = RunPhase.EVALUATION_PENDING
    state.last_diff_sha256 = audit.current_diff_sha256()
    store.save_state(state)
    evaluator = FakeEvaluationCoordinator([])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.resume(saved_task.task_id)

    assert result.status is RunStatus.MANUAL_REVIEW
    assert evaluator.context_hashes == []
    marker = store.load_validation_evidence(saved_task.task_id, 1)
    assert marker.status == "legacy_evidence_unavailable"
    aggregate = json.loads(
        (
            store.run_dir(saved_task.task_id) / "evaluations/round-01/aggregate.json"
        ).read_text(encoding="utf-8")
    )
    assert aggregate["requires_human"] is True
    assert aggregate["schema_version"] == 2
    assert aggregate["validation"]["status"] == "legacy_evidence_unavailable"
    assert "spec_evaluation" not in result.artifacts
    assert "architecture_evaluation" not in result.artifacts


def test_resume_is_idempotent_when_legacy_marker_is_already_frozen(
    repo: Path,
) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(
        repo,
        saved_task,
        evidence_required=False,
    )
    audit = AuditRecorder(
        store.run_dir(saved_task.task_id), state.repo_root, state.base_commit
    )
    state.thread_id = "saved-thread"
    state.turn_count = 1
    passed = ValidationRound(
        round_number=1,
        full_results=[],
        passed=True,
        stage="full",
    )
    store.save_round(saved_task.task_id, passed)
    state.add_round(passed)
    state.phase = RunPhase.EVALUATING
    state.last_diff_sha256 = audit.current_diff_sha256()
    marker = ValidationEvidenceSnapshot.legacy_unavailable(
        task_id=saved_task.task_id,
        validation_round=1,
        base_commit=state.base_commit,
        final_diff_sha256=state.last_diff_sha256,
    )
    store.save_validation_evidence(saved_task.task_id, marker)
    store.save_state(state)
    evaluator = FakeEvaluationCoordinator([])
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = workflow.resume(saved_task.task_id)

    assert result.status is RunStatus.MANUAL_REVIEW
    assert evaluator.context_hashes == []
    assert store.load_validation_evidence(saved_task.task_id, 1) == marker
    events = [
        json.loads(line)
        for line in (store.run_dir(saved_task.task_id) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert (
        sum(item["type"] == "evaluation.legacy_evidence_unavailable" for item in events)
        == 1
    )


def test_resume_rejects_tampered_round_specific_aggregate(repo: Path) -> None:
    first_evaluator = FakeEvaluationCoordinator([{}])
    first_workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([(True, [0])]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=first_evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )
    assert first_workflow.start(task()).status is RunStatus.SUCCESS

    store = StateStore(repo)
    run_dir = store.run_dir("workflow-test")
    aggregate_path = run_dir / "evaluations/round-01/aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["evaluation_input_sha256"] = "0" * 64
    _atomic_write_json(aggregate_path, freeze_aggregate(aggregate))
    state = store.load_state("workflow-test")
    state.status = RunStatus.RUNNING
    state.phase = RunPhase.EVALUATING
    state.finished_at = None
    store.save_state(state)
    evaluator = FakeEvaluationCoordinator([])
    resume_workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = resume_workflow.resume("workflow-test")

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert "evaluation_input_sha256 binding changed" in (
        result.infrastructure_error or ""
    )
    assert evaluator.context_hashes == []


def test_resume_rejects_rehashed_semantic_aggregate_tampering(repo: Path) -> None:
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([(True, [0])]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=FakeEvaluationCoordinator([{}]),  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )
    assert workflow.start(task()).status is RunStatus.SUCCESS

    store = StateStore(repo)
    aggregate_path = (
        store.run_dir("workflow-test") / "evaluations/round-01/aggregate.json"
    )
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["requires_human"] = True
    _atomic_write_json(aggregate_path, freeze_aggregate(aggregate))
    state = store.load_state("workflow-test")
    state.status = RunStatus.RUNNING
    state.phase = RunPhase.EVALUATING
    state.finished_at = None
    store.save_state(state)
    evaluator = FakeEvaluationCoordinator([])
    resume_workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=evaluator,  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = resume_workflow.resume("workflow-test")

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert "evaluation aggregate semantic projection changed" in (
        result.infrastructure_error or ""
    )
    assert evaluator.context_hashes == []


def test_final_evaluated_run_requires_aggregate_even_if_context_is_missing(
    repo: Path,
) -> None:
    workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([(True, [0])]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=FakeEvaluationCoordinator([{}]),  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )
    assert workflow.start(task()).status is RunStatus.SUCCESS

    store = StateStore(repo)
    run_dir = store.run_dir("workflow-test")
    (run_dir / "evaluations/round-01/aggregate.json").unlink()
    (run_dir / "context/evaluation.json").unlink()
    resume_workflow = OrchestrationWorkflow(
        repo,
        client_factory=lambda _root: FakeCodexClient(),
        validator_factory=lambda _root, _baseline: FakeValidator([]),
        context_assembler=FakeContextAssembler(),  # type: ignore[arg-type]
        evaluation_coordinator=FakeEvaluationCoordinator([]),  # type: ignore[arg-type]
        knowledge_actor_id="zhangsan",
    )

    result = resume_workflow.resume("workflow-test")

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert "missing its round aggregate" in (result.infrastructure_error or "")


def test_resume_rebuilds_missing_final_artifacts(repo: Path) -> None:
    saved_task = task()
    store, state = initialize_isolated_run(
        repo,
        saved_task,
        evidence_required=False,
    )
    audit = AuditRecorder(
        store.run_dir(saved_task.task_id), state.repo_root, state.base_commit
    )
    (Path(state.repo_root) / "finished.txt").write_text("done\n", encoding="utf-8")
    state.last_diff_sha256 = audit.current_diff_sha256()
    state.mark_success("?? finished.txt")
    store.save_state(state)

    result = workflow_with(repo, FakeCodexClient(), FakeValidator([])).resume(
        saved_task.task_id
    )

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
        workflow_with(repo, FakeCodexClient(), FakeValidator([(True, [0])])).start(
            second
        )

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


def test_command_infrastructure_error_is_promoted_to_round_and_stops(
    repo: Path,
) -> None:
    class CommandOnlyInfrastructureValidator(FakeValidator):
        def __init__(self) -> None:
            super().__init__([])

        def validate(self, round_number: int) -> ValidationRound:
            self.round_numbers.append(round_number)
            failed_to_start = CommandResult(
                command=["second-check"],
                cwd=str(repo),
                stage="full",
                exit_code=None,
                infrastructure_error="npm could not start",
            )
            return ValidationRound(
                round_number=round_number,
                full_results=[failed_to_start],
                passed=False,
                stage="full",
                failure_summary="command did not start",
            )

    validator = CommandOnlyInfrastructureValidator()

    result = workflow_with(repo, FakeCodexClient(), validator).start(task())

    assert result.status is RunStatus.INFRASTRUCTURE_ERROR
    assert result.failure_count == 0
    assert result.rounds[0].infrastructure_error == "npm could not start"
    store = StateStore(repo)
    evidence = store.load_validation_evidence("workflow-test", 1)
    assert evidence.status == "infra_error"
    aggregate = json.loads(
        (store.run_dir("workflow-test") / "evaluations/aggregate.json").read_text(
            encoding="utf-8"
        )
    )
    assert aggregate["validation_round"] == 1
    assert aggregate["validation"]["status"] == "infra_error"


def test_failed_split_secret_round_repairs_without_checkpoint_conflict(
    repo: Path,
) -> None:
    secret = "opaque-validation-secret"

    class SecretFailureValidator(FakeValidator):
        def __init__(self) -> None:
            super().__init__([])
            self.calls = 0

        def validate(self, round_number: int) -> ValidationRound:
            self.round_numbers.append(round_number)
            self.calls += 1
            if self.calls == 1:
                command = CommandResult(
                    command=["fixed-check", "--token", secret],
                    cwd=str(repo),
                    stage="full",
                    exit_code=1,
                )
                return ValidationRound(
                    round_number=round_number,
                    full_results=[command],
                    passed=False,
                    stage="full",
                    failure_summary=(f"fixed-check --token {secret}: exit code 1"),
                )
            command = CommandResult(
                command=["fixed-check"],
                cwd=str(repo),
                stage="full",
                exit_code=0,
            )
            return ValidationRound(
                round_number=round_number,
                full_results=[command],
                passed=True,
                stage="full",
            )

    client = FakeCodexClient()
    validator = SecretFailureValidator()

    result = workflow_with(repo, client, validator).start(task())

    assert result.status is RunStatus.SUCCESS
    assert validator.round_numbers == [1, 2]
    assert len(client.prompts) == 2
    assert secret not in client.prompts[1]
    assert "--token [REDACTED]" in client.prompts[1]


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
        preflight_error=(error if failure_location == "validation_preflight" else None),
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
