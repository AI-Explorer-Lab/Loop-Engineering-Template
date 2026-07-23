"""Durable isolated Codex -> validate -> repair -> human review workflow."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import subprocess
from typing import Any

from .audit import AuditRecorder, file_sha256
from .codex_client import CodexClient, CodexRunResult
from .context import (
    ContextAssembler,
    ContextSnapshot,
    merge_context_snapshots,
)
from .evaluation import (
    EvaluationCoordinator,
    build_legacy_aggregate,
    build_non_passing_aggregate,
    verify_aggregate_binding,
    verify_control_aggregate,
    verify_evaluation_artifact_binding,
)
from .models import (
    InfrastructureError,
    PromptKind,
    RunPhase,
    RunResult,
    RunState,
    RunStatus,
    TaskSpec,
    ValidationRound,
)
from .policy import ExecutionPolicy
from .report import PromptRenderer, ReportBuilder
from .state import (
    ActiveRunError,
    QueueStore,
    StateStore,
    has_only_plan_artifacts,
    redact_sensitive_data,
    redact_sensitive_text,
    _atomic_write_json,
)
from .validation_runner import ValidationRunner
from .validation_evidence import ValidationEvidenceSnapshot
from .validation_profile import ValidationProfile
from .workspace import WorkspaceInfo, WorkspaceManager


MAX_VALIDATION_FAILURES = 3
MAX_CODEX_TURNS = 3

ClientFactory = Callable[[Path], Any]
ValidatorFactory = Callable[[Path, Mapping[str, str] | None], Any]


class OrchestrationWorkflow:
    """Run one task in a dedicated branch/worktree with complete audit data."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        store: StateStore | None = None,
        client_factory: ClientFactory | None = None,
        validator_factory: ValidatorFactory | None = None,
        prompt_renderer: PromptRenderer | None = None,
        report_builder: ReportBuilder | None = None,
        validation_timeout_seconds: float = 900.0,
        base_ref: str = "HEAD",
        inherited_diff_path: str | Path | None = None,
        inherited_diff_sha256: str = "",
        context_assembler: ContextAssembler | None = None,
        evaluation_coordinator: EvaluationCoordinator | None = None,
        knowledge_actor_id: str = "",
        validation_profile: ValidationProfile | Mapping[str, object] | None = None,
    ) -> None:
        self.control_repo_root = Path(repo_root).expanduser().resolve()
        self.repo_root = self.control_repo_root  # compatibility alias
        if not self.control_repo_root.is_dir():
            raise InfrastructureError(
                f"Project root does not exist: {self.control_repo_root}"
            )
        if validation_timeout_seconds <= 0:
            raise ValueError("validation_timeout_seconds must be greater than zero")

        self.store = store or StateStore(self.control_repo_root)
        self.client_factory = client_factory
        self.validator_factory = validator_factory
        self.prompt_renderer = prompt_renderer or PromptRenderer()
        self.report_builder = report_builder or ReportBuilder()
        self.validation_timeout_seconds = validation_timeout_seconds
        self.inherited_diff_path = (
            None
            if inherited_diff_path is None
            else Path(inherited_diff_path).expanduser().resolve()
        )
        self.inherited_diff_sha256 = str(inherited_diff_sha256)
        self.context_assembler = context_assembler
        self.evaluation_coordinator = evaluation_coordinator
        self.knowledge_actor_id = str(knowledge_actor_id)
        self.validation_profile = (
            validation_profile
            if isinstance(validation_profile, ValidationProfile)
            else ValidationProfile.from_mapping(validation_profile)
        )
        self.workspace_manager = WorkspaceManager(
            self.control_repo_root, base_ref=base_ref
        )

    def start(self, task: TaskSpec) -> RunResult:
        """Create the isolated workspace before starting a Codex thread."""

        lock = self.store.acquire_active_lock(task.task_id)
        state: RunState | None = None
        audit: AuditRecorder | None = None
        try:
            self._assert_no_other_unfinished_task()
            if self.store.run_dir(
                task.task_id
            ).exists() and not has_only_plan_artifacts(
                self.store.run_dir(task.task_id)
            ):
                raise InfrastructureError(
                    f"Task {task.task_id!r} already exists; use resume instead"
                )

            workspace = self.workspace_manager.create(task)
            if self.inherited_diff_path is not None:
                self.workspace_manager.apply_inherited_diff(
                    workspace,
                    self.inherited_diff_path,
                    self.inherited_diff_sha256,
                )
            state = self.store.initialize_run(
                task,
                task_repo_root=workspace.worktree,
                workspace={
                    "base_ref": workspace.base_ref,
                    "base_commit": workspace.base_commit,
                    "task_branch": workspace.task_branch,
                    "worktree_relative_path": workspace.worktree_relative_path,
                    "source_worktree_was_dirty": workspace.source_worktree_was_dirty,
                    "inherited_baseline": self.inherited_diff_path is not None,
                    "inherited_diff_sha256": self.inherited_diff_sha256,
                },
                baseline_git_status=(
                    "source worktree dirty"
                    if workspace.source_worktree_was_dirty
                    else "source worktree clean"
                ),
            )
            if task.queue_id is not None:
                queue_control = QueueStore(self.control_repo_root).load_control(
                    task.queue_id
                )
                if queue_control is not None:
                    self.store.request_control(
                        task.task_id, str(queue_control.get("action", ""))
                    )
            manifest = workspace.manifest()
            manifest["validation_evidence"] = {
                "required": True,
                "schema_version": 1,
            }
            if task.queue_id is not None:
                manifest["queue"] = {
                    "queue_id": task.queue_id,
                    "sequence": task.sequence,
                    "inherited_diff_sha256": self.inherited_diff_sha256,
                }
            self.store.save_manifest(task.task_id, manifest)
            audit = self._audit(state)
            audit.append(
                "run.created",
                {"task_id": task.task_id, "rerun_of": task.rerun_of},
            )
            plan_confirmation = (
                self.store.run_dir(task.task_id) / "plan/confirmation.json"
            )
            if plan_confirmation.is_file():
                confirmation = json.loads(plan_confirmation.read_text(encoding="utf-8"))
                audit.append(
                    "plan.confirmed",
                    {
                        "plan_id": confirmation.get("plan_id"),
                        "reviewer": confirmation.get("reviewer"),
                        "confirmed_plan_sha256": confirmation.get(
                            "confirmed_plan_sha256"
                        ),
                        "manual_edit_count": confirmation.get("manual_edit_count", 0),
                    },
                )
            audit.append(
                "workspace.created",
                {
                    "base_commit": workspace.base_commit,
                    "branch": workspace.task_branch,
                    "worktree": workspace.worktree_relative_path,
                },
            )

            self._assemble_generation_context(task, state, audit)

            policy = ExecutionPolicy(
                self.control_repo_root,
                workspace,
                dependency_paths=self.validation_profile.dependency_paths,
            )
            self.store.save_permissions(task.task_id, policy.requested_snapshot())
            policy.prepare_runtime()
            self.workspace_manager.verify(
                workspace, require_clean=self.inherited_diff_path is None
            )
            audit.append(
                "workspace.verified",
                {
                    "branch": workspace.task_branch,
                    "head": workspace.base_commit,
                    "clean": self.inherited_diff_path is None,
                    "inherited_baseline": self.inherited_diff_path is not None,
                },
            )
            validator = self._make_validator(workspace, policy, None)
            state.baseline_test_hashes = dict(validator.baseline)
            state.protected_test_paths = self._validator_protected_tests(
                validator, fallback=state.baseline_test_hashes
            )
            self.store.save_state(state)
            validator.preflight()

            with self._make_client(workspace, policy, audit, state) as client:
                state.thread_id = client.start_thread()
                self._verify_permissions(task.task_id, state, policy, client, audit)
                state.phase = RunPhase.PROMPT_PENDING
                state.pending_prompt_kind = PromptKind.INITIAL
                self.store.save_state(state)
                return self._drive(task, state, client, validator, audit)
        except ActiveRunError:
            raise
        except Exception as exc:
            error = self._as_infrastructure_error(exc)
            if state is None:
                raise error from exc
            return self._finish_infrastructure_error(task, state, error, audit)
        finally:
            self.store.release_active_lock(lock)

    def resume(self, task_id: str) -> RunResult:
        """Verify the saved workspace/thread before continuing a checkpoint."""

        lock = self.store.acquire_active_lock(task_id)
        state: RunState | None = None
        task: TaskSpec | None = None
        audit: AuditRecorder | None = None
        try:
            self._assert_no_other_unfinished_task(excluding=task_id)
            task = self.store.load_task(task_id)
            state = self.store.load_state(task_id)
            if state.schema_version == 0:
                if (
                    state.status.is_final
                    and (self.store.run_dir(task_id) / "result.json").is_file()
                ):
                    return self.store.load_result(task_id)
                raise InfrastructureError(
                    "legacy_v0 runs are read-only and cannot be resumed"
                )
            result_path = self.store.run_dir(task_id) / "result.json"

            manifest = self.store.load_manifest(task_id)
            workspace = WorkspaceInfo.from_manifest(self.control_repo_root, manifest)
            if (
                workspace.task_id != state.task_id
                or workspace.base_commit != state.base_commit
                or workspace.task_branch != state.task_branch
            ):
                raise InfrastructureError(
                    "run state conflicts with the immutable workspace manifest"
                )
            self.workspace_manager.verify(workspace)
            # Absolute paths in older state files may have been redacted because
            # the standard PWD environment variable was mistaken for a password.
            # The immutable manifest stores the safe relative worktree path, so
            # rehydrate runtime-only paths from that verified source on resume.
            state.repo_root = str(workspace.worktree)
            state.control_repo_root = str(self.control_repo_root)
            state.worktree_relative_path = workspace.worktree_relative_path
            policy = ExecutionPolicy(
                self.control_repo_root,
                workspace,
                dependency_paths=self.validation_profile.dependency_paths,
            )
            policy.prepare_runtime()
            audit = self._audit(state)
            audit.append(
                "workspace.verified",
                {"branch": workspace.task_branch, "head": workspace.base_commit},
            )
            self._assemble_generation_context(task, state, audit)
            if state.status is RunStatus.PAUSED:
                state.reopen_after_pause()
                self.store.clear_control(task_id)
                self.store.save_state(state)
                audit.append("run.resumed", {"checkpoint": state.phase.value})
            if (
                state.phase
                not in {
                    RunPhase.CODEX_TURN,
                    RunPhase.VALIDATING,
                }
                and state.last_diff_sha256
            ):
                if audit.current_diff_sha256() != state.last_diff_sha256:
                    raise InfrastructureError(
                        "Task worktree changed outside the saved workflow checkpoint"
                    )
            if state.status.is_final:
                if state.status in {RunStatus.SUCCESS, RunStatus.MANUAL_REVIEW}:
                    self._verify_final_validation_artifacts(state, audit)
                required_artifacts = (
                    result_path,
                    self.store.run_dir(task_id) / "report.md",
                    self.store.run_dir(task_id) / "changes/files.json",
                    self.store.run_dir(task_id) / "changes/final.diff",
                )
                if all(path.is_file() for path in required_artifacts):
                    saved_result = self.store.load_result(task_id)
                    saved_projection = saved_result.to_dict()
                    saved_result.attach_evaluation_artifacts(
                        self.store.run_dir(task_id)
                    )
                    if (
                        saved_result.to_dict() == saved_projection
                        and saved_result.status is state.status
                        and saved_result.final_diff_sha256 == state.last_diff_sha256
                    ):
                        return saved_result
                return self._persist_final(task, state, audit)

            validator = self._make_validator(
                workspace, policy, state.baseline_test_hashes
            )
            protect_tests = getattr(validator, "protect_tests", None)
            if callable(protect_tests):
                protect_tests(state.protected_test_paths)
            validator.preflight()

            with self._make_client(workspace, policy, audit, state) as client:
                if state.thread_id:
                    resumed_id = client.resume_thread(state.thread_id)
                    if resumed_id != state.thread_id:
                        raise InfrastructureError(
                            "Codex resumed a different thread than the saved thread"
                        )
                else:
                    state.thread_id = client.start_thread()
                    state.phase = RunPhase.PROMPT_PENDING
                    state.pending_prompt_kind = PromptKind.INITIAL
                    self.store.save_state(state)
                self._verify_permissions(task_id, state, policy, client, audit)
                return self._drive(task, state, client, validator, audit)
        except ActiveRunError:
            raise
        except Exception as exc:
            error = self._as_infrastructure_error(exc)
            if state is None or task is None:
                raise error from exc
            return self._finish_infrastructure_error(task, state, error, audit)
        finally:
            self.store.release_active_lock(lock)

    def _drive(
        self,
        task: TaskSpec,
        state: RunState,
        client: Any,
        validator: Any,
        audit: AuditRecorder,
    ) -> RunResult:
        while state.status is RunStatus.RUNNING:
            if self._apply_control(state, audit):
                return self._persist_final(task, state, audit)
            self._recover_round_checkpoint(state, audit)
            if state.status.is_final:
                return self._persist_final(task, state, audit)
            if state.phase is RunPhase.INITIALIZED:
                state.phase = RunPhase.PROMPT_PENDING
                state.pending_prompt_kind = PromptKind.INITIAL
                self.store.save_state(state)
                continue
            if state.phase is RunPhase.PROMPT_PENDING:
                self._ensure_retry_event(state, audit)
                self._run_pending_turn(task, state, client, audit)
                continue
            if state.phase is RunPhase.CODEX_TURN:
                self._recover_or_dispatch_turn(state, client, audit)
                continue
            if state.phase in {RunPhase.VALIDATION_PENDING, RunPhase.VALIDATING}:
                self._run_validation(state, validator, audit)
                if state.status.is_final:
                    return self._persist_final(task, state, audit)
                continue
            if state.phase in {
                RunPhase.EVALUATION_PENDING,
                RunPhase.EVALUATING,
            }:
                self._run_evaluation(task, state, audit)
                if state.status.is_final:
                    return self._persist_final(task, state, audit)
                continue
            if state.phase is RunPhase.COMPLETED:
                return self._persist_final(task, state, audit)
            raise InfrastructureError(f"Unsupported workflow phase: {state.phase}")
        return self._persist_final(task, state, audit)

    def _apply_control(self, state: RunState, audit: AuditRecorder) -> bool:
        """Apply a durable pause/cancel request only between unsafe operations."""

        request = self.store.load_control(state.task_id)
        if request is None:
            return False
        action = str(request.get("action", ""))
        if action == "pause":
            state.mark_paused()
            event_type = "run.paused"
        elif action == "cancel":
            state.mark_cancelled()
            event_type = "run.cancelled"
        else:
            raise InfrastructureError(f"Unsupported control action: {action}")
        self.store.save_state(state)
        audit.append(
            event_type,
            {
                "requested_at": request.get("requested_at"),
                "checkpoint": state.phase.value,
            },
        )
        self.store.clear_control(state.task_id)
        return True

    def _run_pending_turn(
        self,
        task: TaskSpec,
        state: RunState,
        client: Any,
        audit: AuditRecorder,
    ) -> None:
        if state.cycle_turn_count >= MAX_CODEX_TURNS:
            state.mark_manual_review(self._safe_git_summary(Path(state.repo_root)))
            self.store.save_state(state)
            return

        prompt_kind = state.pending_prompt_kind
        if prompt_kind is PromptKind.INITIAL:
            prompt = self.prompt_renderer.initial_prompt(task, state)
        elif prompt_kind is PromptKind.REPAIR:
            if not state.rounds or state.cycle_failure_count not in {1, 2}:
                raise InfrastructureError(
                    "No failed validation round is available to repair"
                )
            prompt = self.prompt_renderer.repair_prompt(
                task,
                state,
                state.rounds[-1],
                changed_files=audit.changed_paths(),
                diff_sha256=audit.current_diff_sha256(),
            )
        elif prompt_kind is PromptKind.REVIEW_REPAIR:
            latest_review = self.store.load_latest_review(task.task_id)
            if (
                latest_review is None
                or latest_review.decision.value != "changes_requested"
            ):
                raise InfrastructureError(
                    "No human change request is available to repair"
                )
            prompt = self.prompt_renderer.review_repair_prompt(
                task,
                state,
                latest_review.comment,
                changed_files=audit.changed_paths(),
                diff_sha256=audit.current_diff_sha256(),
            )
        elif prompt_kind is PromptKind.EVALUATION_REPAIR:
            if not state.pending_evaluation_summary:
                raise InfrastructureError(
                    "No independent evaluation failure is available to repair"
                )
            prompt = self.prompt_renderer.evaluation_repair_prompt(
                task,
                state,
                state.pending_evaluation_summary,
                changed_files=audit.changed_paths(),
                diff_sha256=audit.current_diff_sha256(),
            )
        else:
            raise InfrastructureError("No pending Codex prompt is recorded")

        context = self._load_context(
            self.store.run_dir(task.task_id) / "context" / "generation.json"
        )
        if context is not None:
            prompt = f"{prompt}\n\n{context.prompt_block()}"

        turn_number = state.turn_count + 1
        state.last_diff_sha256 = audit.current_diff_sha256()
        checkpoint_paths = audit.changed_paths()
        prompt_path = audit.save_prompt(turn_number, prompt)
        audit.append(
            "turn.started",
            {
                "prompt_kind": prompt_kind.value,
                "prompt_path": prompt_path.relative_to(audit.run_dir).as_posix(),
                "diff_sha256": state.last_diff_sha256,
                "changed_paths": checkpoint_paths,
            },
            source="codex",
            turn_number=turn_number,
            redacted=True,
        )
        state.turn_count = turn_number
        state.cycle_turn_count += 1
        state.phase = RunPhase.CODEX_TURN
        state.pending_prompt_kind = None
        self.store.save_state(state)

        result: CodexRunResult = client.run(prompt)
        self._complete_turn(state, result, audit, recovered=False)

    def _recover_or_dispatch_turn(
        self,
        state: RunState,
        client: Any,
        audit: AuditRecorder,
    ) -> None:
        turn_number = state.turn_count
        recorded_sha = audit.latest_recorded_worktree_diff_sha256(turn_number)
        recovered: CodexRunResult | None = client.verify_turn_completed(turn_number)
        if recovered is None:
            # The prompt was durably saved but App Server has no matching turn,
            # so this is the one safe point where dispatch may be retried.
            result: CodexRunResult = client.run(audit.load_prompt(turn_number))
            self._complete_turn(state, result, audit, recovered=True)
            return
        if not recovered.history_complete:
            raise InfrastructureError(
                "Completed Codex turn history is incomplete and cannot be audited"
            )

        current_sha = audit.current_diff_sha256()
        if recorded_sha and current_sha != recorded_sha:
            raise InfrastructureError(
                "Task worktree changed after the last recorded Codex file event"
            )

        audit.backfill_completed_items(turn_number, recovered.items)
        if not recorded_sha:
            allowed_paths = audit.turn_checkpoint_paths(turn_number)
            allowed_paths.update(audit.codex_changed_paths(turn_number))
            unexpected = set(audit.changed_paths()) - allowed_paths
            if unexpected:
                raise InfrastructureError(
                    "Task worktree contains changes not declared by the recovered "
                    f"Codex turn: {', '.join(sorted(unexpected))}"
                )
        self._complete_turn(state, recovered, audit, recovered=True)

    def _complete_turn(
        self,
        state: RunState,
        result: CodexRunResult,
        audit: AuditRecorder,
        *,
        recovered: bool,
    ) -> None:
        turn_number = state.turn_count
        backfilled = audit.backfill_completed_items(turn_number, result.items)
        response_path = audit.save_response(turn_number, result.final_response)
        if not audit.has_event("turn.completed", turn_number=turn_number):
            audit.append(
                "turn.completed",
                {
                    "turn_id": result.turn_id,
                    "response_path": response_path.relative_to(
                        audit.run_dir
                    ).as_posix(),
                    "usage": result.usage,
                    "recovered": recovered,
                    "backfilled_items": backfilled,
                },
                source="codex",
                turn_number=turn_number,
                redacted=True,
            )
        state.last_diff_sha256 = audit.current_diff_sha256()
        state.phase = RunPhase.VALIDATION_PENDING
        state.pending_prompt_kind = None
        self.store.save_state(state)

    def _run_validation(
        self, state: RunState, validator: Any, audit: AuditRecorder
    ) -> None:
        if state.active_validation_round is None:
            round_number = len(state.rounds) + 1
            state.phase = RunPhase.VALIDATING
            state.active_validation_round = round_number
            state.validation_start_diff_sha256 = audit.current_diff_sha256()
            state.last_diff_sha256 = state.validation_start_diff_sha256
            self.store.save_state(state)
        else:
            round_number = state.active_validation_round
            if round_number != len(state.rounds) + 1:
                raise InfrastructureError(
                    "active validation round is not the next durable round"
                )
            current_diff_sha256 = audit.current_diff_sha256()
            if current_diff_sha256 != state.validation_start_diff_sha256:
                raise InfrastructureError(
                    "Task worktree changed after validation started"
                )
        if not audit.has_event("validation.started", round_number=round_number):
            audit.append(
                "validation.started",
                {"diff_sha256": state.validation_start_diff_sha256},
                round_number=round_number,
            )

        validation_round: ValidationRound = validator.validate(round_number)
        state.protected_test_paths = self._validator_protected_tests(
            validator, fallback=state.protected_test_paths
        )
        for command_index, command_result in enumerate(
            validation_round.command_results, start=1
        ):
            log_path = self.store.write_command_log(
                state.task_id, round_number, command_index, command_result
            )
            audit.append(
                "validation.command.completed",
                {
                    "command": command_result.command,
                    "cwd": command_result.cwd,
                    "exit_code": command_result.exit_code,
                    "duration_seconds": command_result.duration_seconds,
                    "timed_out": command_result.timed_out,
                    "infrastructure_error": command_result.infrastructure_error,
                    "log_path": log_path.relative_to(audit.run_dir).as_posix(),
                    "log_sha256": file_sha256(log_path),
                },
                round_number=round_number,
                redacted=True,
            )
        command_infrastructure_error = next(
            (
                redact_sensitive_text(str(result.infrastructure_error))
                for result in validation_round.command_results
                if result.infrastructure_error
            ),
            None,
        )
        if command_infrastructure_error:
            validation_round.passed = False
            if not validation_round.infrastructure_error:
                validation_round.infrastructure_error = command_infrastructure_error
            validation_round.failure_summary = validation_round.infrastructure_error
        final_diff_sha256 = audit.current_diff_sha256()
        if final_diff_sha256 != state.validation_start_diff_sha256:
            validation_round.passed = False
            validation_round.infrastructure_error = (
                "Task worktree changed while fixed validation commands were running"
            )
            validation_round.failure_summary = validation_round.infrastructure_error
        state.last_diff_sha256 = final_diff_sha256
        evidence = ValidationEvidenceSnapshot.from_round(
            validation_round,
            task_id=state.task_id,
            base_commit=state.base_commit,
            final_diff_sha256=final_diff_sha256,
            control_repo_root=self.control_repo_root,
            run_dir=audit.run_dir,
        )
        round_path = self.store.save_round(state.task_id, validation_round)
        evidence_path = self.store.save_validation_evidence(state.task_id, evidence)
        audit.append(
            "validation.evidence.frozen",
            {
                "status": evidence.status,
                "evidence_ids": [item.evidence_id for item in evidence.commands],
                "validation_round_path": round_path.relative_to(
                    audit.run_dir
                ).as_posix(),
                "validation_evidence_path": evidence_path.relative_to(
                    audit.run_dir
                ).as_posix(),
                "validation_evidence_sha256": evidence.snapshot_sha256,
                "diff_sha256": final_diff_sha256,
            },
            round_number=round_number,
        )
        if evidence.status in {"fail", "infra_error"}:
            self._project_non_passing_validation(state, evidence)
        state.add_round(validation_round)
        state.active_validation_round = None
        state.validation_start_diff_sha256 = ""
        self.store.save_state(state)
        audit.append(
            "validation.completed",
            {
                "passed": validation_round.passed,
                "failure_summary": validation_round.failure_summary,
                "diff_sha256": state.last_diff_sha256,
                "validation_evidence_path": evidence_path.relative_to(
                    audit.run_dir
                ).as_posix(),
                "validation_evidence_sha256": evidence.snapshot_sha256,
            },
            round_number=round_number,
            redacted=True,
        )
        if evidence.status == "infra_error":
            raise InfrastructureError(
                validation_round.infrastructure_error
                or evidence.round_infrastructure_error
                or "Fixed validation reported an infrastructure error"
            )
        if validation_round.passed:
            if self.evaluation_coordinator is None:
                state.mark_success(self._safe_git_summary(Path(state.repo_root)))
            else:
                state.phase = RunPhase.EVALUATION_PENDING
                state.pending_prompt_kind = None
        elif state.cycle_failure_count >= MAX_VALIDATION_FAILURES:
            state.mark_manual_review(self._safe_git_summary(Path(state.repo_root)))
        else:
            audit.append(
                "retry.scheduled",
                {
                    "failure_count": state.cycle_failure_count,
                    "next_turn": state.turn_count + 1,
                },
                round_number=round_number,
            )
        self.store.save_state(state)

    def _recover_round_checkpoint(self, state: RunState, audit: AuditRecorder) -> None:
        """Finish a validation projection already saved before a crash."""

        required = self._validation_evidence_required(state.task_id)
        active_round = state.active_validation_round
        if active_round is not None:
            round_path = self.store.round_path(state.task_id, active_round)
            evidence_path = self.store.validation_evidence_path(
                state.task_id, active_round
            )
            has_round = round_path.is_file()
            has_evidence = evidence_path.is_file()
            if not has_round and not has_evidence:
                return
            if not has_round or not has_evidence:
                raise InfrastructureError(
                    "validation checkpoint is incomplete: round and evidence must coexist"
                )
            latest = self.store.load_round(state.task_id, active_round)
            evidence = self.store.load_validation_evidence(state.task_id, active_round)
            if audit.current_diff_sha256() != evidence.final_diff_sha256:
                raise InfrastructureError(
                    "Task worktree changed after validation evidence was frozen"
                )
            self._verify_validation_evidence(
                state,
                audit,
                latest,
                evidence,
                expected_diff_sha256=evidence.final_diff_sha256,
            )
            if (
                evidence.final_diff_sha256 != state.validation_start_diff_sha256
                and evidence.status != "infra_error"
            ):
                raise InfrastructureError(
                    "validation Diff changed without an infrastructure-error snapshot"
                )
            if evidence.status in {"fail", "infra_error"}:
                self._project_non_passing_validation(state, evidence)
            matching = [
                item for item in state.rounds if item.round_number == active_round
            ]
            round_added = False
            if matching:
                if redact_sensitive_data(
                    matching[0].to_dict(include_output=False)
                ) != redact_sensitive_data(latest.to_dict(include_output=False)):
                    raise InfrastructureError(
                        "state validation round conflicts with frozen round artifact"
                    )
            else:
                state.add_round(latest)
                round_added = True
            if evidence.status == "infra_error":
                if round_added and not latest.infrastructure_error:
                    state.failure_count = max(0, state.failure_count - 1)
                    state.cycle_failure_count = max(0, state.cycle_failure_count - 1)
                infrastructure_error = (
                    evidence.round_infrastructure_error
                    or next(
                        (
                            item.infrastructure_error
                            for item in evidence.commands
                            if item.infrastructure_error
                        ),
                        None,
                    )
                    or "Fixed validation reported an infrastructure error"
                )
                state.mark_infrastructure_error(infrastructure_error)
            state.active_validation_round = None
            state.validation_start_diff_sha256 = ""
            state.last_diff_sha256 = evidence.final_diff_sha256
            self.store.save_state(state)
        elif not state.rounds:
            return
        else:
            latest = state.rounds[-1]
            round_path = self.store.round_path(state.task_id, latest.round_number)
            evidence_path = self.store.validation_evidence_path(
                state.task_id, latest.round_number
            )
            if required:
                if not round_path.is_file() or not evidence_path.is_file():
                    raise InfrastructureError(
                        "required validation evidence checkpoint is missing"
                    )
                persisted = self.store.load_round(state.task_id, latest.round_number)
                if redact_sensitive_data(
                    persisted.to_dict(include_output=False)
                ) != redact_sensitive_data(latest.to_dict(include_output=False)):
                    raise InfrastructureError(
                        "state validation round conflicts with frozen round artifact"
                    )
                evidence = self.store.load_validation_evidence(
                    state.task_id, latest.round_number
                )
                self._verify_validation_evidence(
                    state,
                    audit,
                    latest,
                    evidence,
                    expected_diff_sha256=evidence.final_diff_sha256,
                )
            elif not audit.has_event(
                "validation.started", round_number=latest.round_number
            ):
                return

        round_number = latest.round_number
        evidence = (
            self.store.load_validation_evidence(state.task_id, round_number)
            if self.store.validation_evidence_path(
                state.task_id, round_number
            ).is_file()
            else None
        )
        if evidence is not None and not audit.has_event(
            "validation.evidence.frozen", round_number=round_number
        ):
            audit.append(
                "validation.evidence.frozen",
                {
                    "status": evidence.status,
                    "evidence_ids": [item.evidence_id for item in evidence.commands],
                    "validation_evidence_path": (
                        self.store.validation_evidence_path(state.task_id, round_number)
                        .relative_to(audit.run_dir)
                        .as_posix()
                    ),
                    "validation_evidence_sha256": evidence.snapshot_sha256,
                    "diff_sha256": evidence.final_diff_sha256,
                    "recovered": True,
                },
                round_number=round_number,
            )
        if not audit.has_event("validation.completed", round_number=round_number):
            audit.append(
                "validation.completed",
                {
                    "passed": latest.passed,
                    "failure_summary": latest.failure_summary,
                    "diff_sha256": state.last_diff_sha256,
                    "validation_evidence_path": (
                        None
                        if evidence is None
                        else self.store.validation_evidence_path(
                            state.task_id, round_number
                        )
                        .relative_to(audit.run_dir)
                        .as_posix()
                    ),
                    "validation_evidence_sha256": (
                        None if evidence is None else evidence.snapshot_sha256
                    ),
                    "recovered": True,
                },
                round_number=round_number,
                redacted=True,
            )
        if evidence is not None and evidence.status in {"fail", "infra_error"}:
            self._project_non_passing_validation(state, evidence)
        if state.status is RunStatus.INFRASTRUCTURE_ERROR:
            return
        if latest.passed and state.phase is RunPhase.VALIDATING:
            if self.evaluation_coordinator is None:
                state.mark_success(self._safe_git_summary(Path(state.repo_root)))
            else:
                state.phase = RunPhase.EVALUATION_PENDING
                state.pending_prompt_kind = None
            self.store.save_state(state)
        elif not latest.passed and state.cycle_failure_count >= MAX_VALIDATION_FAILURES:
            state.mark_manual_review(self._safe_git_summary(Path(state.repo_root)))
            self.store.save_state(state)

    @staticmethod
    def _ensure_retry_event(state: RunState, audit: AuditRecorder) -> None:
        if (
            state.pending_prompt_kind
            not in {PromptKind.REPAIR, PromptKind.EVALUATION_REPAIR}
            or not state.rounds
            or state.cycle_failure_count >= MAX_VALIDATION_FAILURES
        ):
            return
        round_number = state.rounds[-1].round_number
        if not audit.has_event("validation.started", round_number=round_number):
            return
        if not audit.has_event("retry.scheduled", round_number=round_number):
            audit.append(
                "retry.scheduled",
                {
                    "failure_count": state.cycle_failure_count,
                    "next_turn": state.turn_count + 1,
                    "recovered": True,
                },
                round_number=round_number,
            )

    def _run_evaluation(
        self,
        task: TaskSpec,
        state: RunState,
        audit: AuditRecorder,
    ) -> None:
        if self.evaluation_coordinator is None:
            state.mark_success(self._safe_git_summary(Path(state.repo_root)))
            self.store.save_state(state)
            return
        if not state.rounds or not state.rounds[-1].passed:
            raise InfrastructureError("evaluation requires a passed validation round")
        round_number = state.rounds[-1].round_number
        state.phase = RunPhase.EVALUATING
        self.store.save_state(state)
        run_dir = self.store.run_dir(task.task_id)
        evidence_path = self.store.validation_evidence_path(task.task_id, round_number)
        evidence_relative_path = evidence_path.relative_to(run_dir).as_posix()
        if not evidence_path.is_file():
            if self._validation_evidence_required(task.task_id):
                raise InfrastructureError(
                    "required validation evidence is missing before evaluation"
                )
            legacy = ValidationEvidenceSnapshot.legacy_unavailable(
                task_id=task.task_id,
                validation_round=round_number,
                base_commit=state.base_commit,
                final_diff_sha256=audit.current_diff_sha256(),
            )
            self.store.save_validation_evidence(task.task_id, legacy)
            self._finish_legacy_evaluation(
                state,
                audit,
                legacy,
                evidence_relative_path=evidence_relative_path,
            )
            return

        evidence = self.store.load_validation_evidence(task.task_id, round_number)
        if evidence.status == "legacy_evidence_unavailable":
            if self._validation_evidence_required(task.task_id):
                raise InfrastructureError(
                    "required validation evidence cannot be a legacy marker"
                )
            self._finish_legacy_evaluation(
                state,
                audit,
                evidence,
                evidence_relative_path=evidence_relative_path,
            )
            return
        self._verify_validation_evidence(
            state,
            audit,
            state.rounds[-1],
            evidence,
            expected_diff_sha256=audit.current_diff_sha256(),
        )
        if evidence.status != "pass":
            raise InfrastructureError(
                "evaluation cannot consume non-passing validation evidence"
            )
        context = self._assemble_evaluation_context(task, state, audit)
        changed_files = audit.current_changed_files()
        diff_text = audit.current_diff_text()
        binding, _normalized_files = EvaluationCoordinator.input_binding(
            task=task,
            context=context,
            changed_files=changed_files,
            diff_text=diff_text,
            validation_evidence=evidence,
        )
        round_root = run_dir / "evaluations" / f"round-{round_number:02d}"
        aggregate_path = round_root / "aggregate.json"
        if aggregate_path.is_file():
            value = json.loads(aggregate_path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise InfrastructureError(
                    "evaluation aggregate must contain one object"
                )
            aggregate = dict(value)
            verify_aggregate_binding(
                aggregate,
                binding=binding,
                evidence_path=evidence_relative_path,
                evidence_ids=[item.evidence_id for item in evidence.commands],
            )
        else:
            self.evaluation_coordinator.event_sink = lambda event_type, payload: (
                audit.append(
                    event_type,
                    payload,
                    source="evaluator",
                    round_number=round_number,
                )
            )
            aggregate = self.evaluation_coordinator.evaluate(
                task=task,
                context=context,
                changed_files=changed_files,
                diff_text=diff_text,
                validation_evidence=evidence,
                validation_evidence_path=evidence_relative_path,
                artifact_root=run_dir / "evaluations",
            )
            verify_aggregate_binding(
                aggregate,
                binding=binding,
                evidence_path=evidence_relative_path,
                evidence_ids=[item.evidence_id for item in evidence.commands],
            )
        role_artifacts: dict[str, Mapping[str, Any]] = {}
        for filename in ("spec.json", "architecture.json"):
            artifact_path = round_root / filename
            if not artifact_path.is_file():
                raise InfrastructureError(
                    f"evaluation role artifact is missing: {filename}"
                )
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            if not isinstance(artifact, Mapping):
                raise InfrastructureError(
                    f"evaluation role artifact must be an object: {filename}"
                )
            verify_evaluation_artifact_binding(
                artifact,
                binding=binding,
                evidence_path=evidence_relative_path,
            )
            role_artifacts[filename] = artifact
            _atomic_write_json(
                run_dir / "evaluations" / filename,
                artifact,
            )
        EvaluationCoordinator.verify_frozen_evaluation(
            task=task,
            context=context,
            changed_files=changed_files,
            validation_evidence=evidence,
            validation_evidence_path=evidence_relative_path,
            binding=binding,
            spec_artifact=role_artifacts["spec.json"],
            architecture_artifact=role_artifacts["architecture.json"],
            aggregate=aggregate,
        )
        _atomic_write_json(
            run_dir / "evaluations" / "aggregate.json",
            aggregate,
        )
        if bool(aggregate.get("requires_repair")):
            summary = json.dumps(
                aggregate.get("blocking_findings", []),
                ensure_ascii=False,
                sort_keys=True,
            )
            state.pending_evaluation_summary = summary
            state.failure_count += 1
            state.cycle_failure_count += 1
            if state.cycle_turn_count >= MAX_CODEX_TURNS:
                state.mark_manual_review(self._safe_git_summary(Path(state.repo_root)))
            else:
                state.phase = RunPhase.PROMPT_PENDING
                state.pending_prompt_kind = PromptKind.EVALUATION_REPAIR
                audit.append(
                    "retry.scheduled",
                    {
                        "reason": "independent_evaluation",
                        "failure_count": state.cycle_failure_count,
                        "next_turn": state.turn_count + 1,
                    },
                    round_number=round_number,
                )
        elif bool(aggregate.get("requires_human")):
            state.pending_evaluation_summary = ""
            state.mark_manual_review(self._safe_git_summary(Path(state.repo_root)))
        else:
            state.pending_evaluation_summary = ""
            state.mark_success(self._safe_git_summary(Path(state.repo_root)))
        self.store.save_state(state)

    def _assemble_generation_context(
        self,
        task: TaskSpec,
        state: RunState,
        audit: AuditRecorder,
    ) -> ContextSnapshot | None:
        if self.context_assembler is None:
            return None
        self._bind_context_events(audit)
        return self.context_assembler.assemble(
            path=self.store.run_dir(task.task_id) / "context" / "generation.json",
            stage="generation",
            query=" ".join([task.requirement, *task.acceptance_criteria]),
            actor=self.knowledge_actor_id,
            include_memory=True,
        )

    def _assemble_evaluation_context(
        self,
        task: TaskSpec,
        state: RunState,
        audit: AuditRecorder,
    ) -> ContextSnapshot:
        if self.context_assembler is None:
            raise InfrastructureError("evaluation context assembler is unavailable")
        self._bind_context_events(audit)
        context_root = self.store.run_dir(task.task_id) / "context"
        changed_paths = audit.changed_paths()
        query = " ".join([task.requirement, *task.acceptance_criteria])
        specification = self.context_assembler.assemble(
            path=context_root / "spec_evaluation.json",
            stage="spec_evaluation",
            query=query,
            actor=self.knowledge_actor_id,
            changed_paths=changed_paths,
        )
        architecture = self.context_assembler.assemble(
            path=context_root / "architecture_evaluation.json",
            stage="architecture_evaluation",
            query=query,
            actor=self.knowledge_actor_id,
            changed_paths=changed_paths,
        )
        target = context_root / "evaluation.json"
        if target.is_file():
            merged = ContextSnapshot.from_dict(
                json.loads(target.read_text(encoding="utf-8"))
            )
            merged.verify_hash()
            return merged
        merged = merge_context_snapshots("evaluation", specification, architecture)
        _atomic_write_json(target, merged.to_dict())
        audit.append(
            "context.assembled",
            {
                "stage": "evaluation",
                "snapshot_sha256": merged.snapshot_sha256,
                "knowledge_count": len(merged.knowledge),
                "skill_count": len(merged.skills),
            },
        )
        return merged

    def _bind_context_events(self, audit: AuditRecorder) -> None:
        if self.context_assembler is None:
            return
        sink = lambda event_type, payload: audit.append(event_type, payload)
        self.context_assembler.event_sink = sink
        self.context_assembler.knowledge.client.event_sink = sink
        self.context_assembler.skills.client.event_sink = sink

    @staticmethod
    def _load_context(path: Path) -> ContextSnapshot | None:
        if not path.is_file():
            return None
        snapshot = ContextSnapshot.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
        snapshot.verify_hash()
        return snapshot

    def _verify_permissions(
        self,
        task_id: str,
        state: RunState,
        policy: ExecutionPolicy,
        client: Any,
        audit: AuditRecorder,
    ) -> None:
        client.verify_thread_workspace()
        effective_config = client.effective_config()
        snapshot = policy.verify_effective(effective_config)
        self.store.save_permissions(task_id, snapshot)
        manifest = self.store.load_manifest(task_id)
        runtime = manifest.get("runtime")
        if isinstance(runtime, dict):
            reported_model = effective_config.get("model")
            runtime["model"] = (
                str(reported_model).strip() if reported_model else "not-reported"
            )
            self.store.save_manifest(task_id, manifest)
        state.permission_verified = True
        self.store.save_state(state)
        audit.append(
            "permissions.verified",
            snapshot["effective"],
        )

    def _make_client(
        self,
        workspace: WorkspaceInfo,
        policy: ExecutionPolicy,
        audit: AuditRecorder,
        state: RunState,
    ) -> Any:
        if self.client_factory is None:
            return CodexClient(
                workspace.worktree,
                policy=policy,
                event_sink=lambda notification: audit.record_codex_notification(
                    state.turn_count, notification
                ),
                permission_denial_sink=lambda method, params: (
                    audit.record_permission_denial(state.turn_count, method, params)
                ),
            )
        return self.client_factory(workspace.worktree)

    def _make_validator(
        self,
        workspace: WorkspaceInfo,
        policy: ExecutionPolicy,
        baseline: Mapping[str, str] | None,
    ) -> Any:
        if self.validator_factory is not None:
            return self.validator_factory(workspace.worktree, baseline)
        return ValidationRunner(
            workspace.worktree,
            timeout_seconds=self.validation_timeout_seconds,
            baseline_hashes=baseline,
            environment=policy.validation_environment(),
            command_prefix=policy.validation_command_prefix(),
            validation_profile=self.validation_profile,
        )

    def _finish_infrastructure_error(
        self,
        task: TaskSpec,
        state: RunState,
        error: Exception,
        audit: AuditRecorder | None,
    ) -> RunResult:
        message = redact_sensitive_text(str(error) or type(error).__name__)
        state.mark_infrastructure_error(
            message, self._safe_git_summary(Path(state.repo_root))
        )
        self.store.save_state(state)
        recorder = audit or self._audit(state)
        return self._persist_final(task, state, recorder)

    def _persist_final(
        self, task: TaskSpec, state: RunState, audit: AuditRecorder
    ) -> RunResult:
        if not state.status.is_final:
            raise InfrastructureError("Cannot write a final report for a running task")
        require_final_integrity = state.status in {
            RunStatus.SUCCESS,
            RunStatus.MANUAL_REVIEW,
        }
        verified_diff_sha256 = state.last_diff_sha256
        if require_final_integrity:
            self._verify_final_validation_artifacts(state, audit)
        changes = audit.capture_final_changes()
        final_diff = changes.get("final_diff", {})
        captured_diff_sha256 = str(final_diff.get("raw_sha256", ""))
        if require_final_integrity and captured_diff_sha256 != verified_diff_sha256:
            raise InfrastructureError(
                "Task worktree changed while final artifacts were being captured"
            )
        state.last_diff_sha256 = captured_diff_sha256
        cumulative_diff = changes.get("cumulative_diff", {})
        state.diff_redaction_count = max(
            int(final_diff.get("redaction_count", 0)),
            int(
                cumulative_diff.get("redaction_count", 0)
                if isinstance(cumulative_diff, Mapping)
                else 0
            ),
        )
        self.store.save_state(state)
        if state.status is RunStatus.INFRASTRUCTURE_ERROR:
            event_type = "run.failed"
        elif state.status is RunStatus.PAUSED:
            event_type = "run.paused"
        elif state.status is RunStatus.CANCELLED:
            event_type = "run.cancelled"
        else:
            event_type = "run.completed"
        if not audit.has_event(event_type):
            audit.append(
                event_type,
                {
                    "machine_status": state.status.value,
                    "review_status": state.review_status.value,
                    "diff_sha256": state.last_diff_sha256,
                },
            )
        permissions_path = self.store.run_dir(task.task_id) / "permissions.json"
        permissions = (
            self.store.load_permissions(task.task_id)
            if permissions_path.is_file()
            else {"effective": {"verified": False}}
        )
        review_path = self.store.run_dir(task.task_id) / "review.json"
        review = (
            self.store.load_latest_review(task.task_id)
            if task.queue_id is not None
            else (
                self.store.load_review(task.task_id) if review_path.is_file() else None
            )
        )
        result, report = self.report_builder.build(
            task,
            state,
            permissions=permissions,
            changes=changes,
            review=review,
            denied_event_count=audit.denied_event_count(),
        )
        result.attach_evaluation_artifacts(self.store.run_dir(task.task_id))
        self.store.save_result(result)
        self.store.save_report(task.task_id, report)
        return result

    def _audit(self, state: RunState) -> AuditRecorder:
        if not state.base_commit:
            raise InfrastructureError("Run state has no baseline commit")
        return AuditRecorder(
            self.store.run_dir(state.task_id),
            state.repo_root,
            state.base_commit,
            inherited_baseline=state.inherited_baseline,
            queue_task=state.queue_id is not None,
        )

    def _assert_no_other_unfinished_task(self, *, excluding: str | None = None) -> None:
        unfinished = self.store.unfinished_task_ids(excluding=excluding)
        if unfinished:
            raise ActiveRunError(
                "an unfinished task must be resumed or reviewed before starting "
                f"another task (task_id={', '.join(unfinished)})"
            )
        unfinished_queues = QueueStore(self.control_repo_root).unfinished_queue_ids(
            excluding=self.store.queue_id
        )
        if unfinished_queues:
            raise ActiveRunError(
                "an unfinished task queue must be resumed or reviewed before "
                f"starting another task (queue_id={', '.join(unfinished_queues)})"
            )

    @staticmethod
    def _as_infrastructure_error(error: Exception) -> InfrastructureError:
        if isinstance(error, InfrastructureError):
            return error
        detail = redact_sensitive_text(str(error)).strip()
        suffix = f": {detail}" if detail else ""
        return InfrastructureError(
            f"Orchestrator infrastructure failure ({type(error).__name__}){suffix}"
        )

    @staticmethod
    def _validator_protected_tests(
        validator: Any, *, fallback: Mapping[str, str] | list[str]
    ) -> list[str]:
        protected = getattr(validator, "protected_test_paths", None)
        if protected is None:
            values = fallback.keys() if isinstance(fallback, Mapping) else fallback
            return sorted({str(path) for path in values})
        return sorted({str(path) for path in protected})

    def _validation_evidence_required(self, task_id: str) -> bool:
        manifest = self.store.load_manifest(task_id)
        if "validation_evidence" not in manifest:
            return False
        value = manifest.get("validation_evidence")
        if not isinstance(value, Mapping):
            raise InfrastructureError(
                "manifest validation_evidence policy must be an object"
            )
        if value.get("required") is not True or value.get("schema_version") != 1:
            raise InfrastructureError(
                "manifest validation_evidence policy is unsupported"
            )
        return True

    def _verify_validation_evidence(
        self,
        state: RunState,
        audit: AuditRecorder,
        validation_round: ValidationRound,
        evidence: ValidationEvidenceSnapshot,
        *,
        expected_diff_sha256: str,
    ) -> None:
        evidence.verify_binding(
            task_id=state.task_id,
            validation_round=validation_round.round_number,
            base_commit=state.base_commit,
            final_diff_sha256=expected_diff_sha256,
        )
        evidence.verify_round(
            validation_round,
            control_repo_root=self.control_repo_root,
            run_dir=audit.run_dir,
        )
        evidence.verify_logs(audit.run_dir)

    def _finish_legacy_evaluation(
        self,
        state: RunState,
        audit: AuditRecorder,
        evidence: ValidationEvidenceSnapshot,
        *,
        evidence_relative_path: str,
    ) -> None:
        evidence.verify_binding(
            task_id=state.task_id,
            validation_round=state.rounds[-1].round_number,
            base_commit=state.base_commit,
            final_diff_sha256=audit.current_diff_sha256(),
        )
        evaluation_root = self.store.run_dir(state.task_id) / "evaluations"
        round_root = evaluation_root / f"round-{evidence.validation_round:02d}"
        aggregate_path = round_root / "aggregate.json"
        legacy_projection_completed = audit.has_event(
            "evaluation.legacy_evidence_unavailable",
            round_number=evidence.validation_round,
        )
        if legacy_projection_completed:
            if not aggregate_path.is_file():
                raise InfrastructureError("legacy aggregate checkpoint is missing")
            aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
            if not isinstance(aggregate, Mapping):
                raise InfrastructureError("legacy aggregate must contain one object")
            verify_control_aggregate(
                aggregate,
                evidence=evidence,
                evidence_path=evidence_relative_path,
            )
        else:
            # A pre-evidence evaluator may already have written an unbound aggregate
            # at this path. It is historical input, not a reusable checkpoint.
            aggregate = build_legacy_aggregate(
                evidence,
                evidence_path=evidence_relative_path,
            )
            _atomic_write_json(aggregate_path, aggregate)
        _atomic_write_json(evaluation_root / "aggregate.json", aggregate)
        for filename in ("spec.json", "architecture.json"):
            (evaluation_root / filename).unlink(missing_ok=True)
        if not legacy_projection_completed:
            audit.append(
                "evaluation.legacy_evidence_unavailable",
                {
                    "validation_round": evidence.validation_round,
                    "validation_evidence_path": evidence_relative_path,
                    "validation_evidence_sha256": evidence.snapshot_sha256,
                },
                source="evaluator",
                round_number=evidence.validation_round,
            )
        state.pending_evaluation_summary = ""
        state.mark_manual_review(self._safe_git_summary(Path(state.repo_root)))
        self.store.save_state(state)

    def _project_non_passing_validation(
        self,
        state: RunState,
        evidence: ValidationEvidenceSnapshot,
    ) -> None:
        run_dir = self.store.run_dir(state.task_id)
        evidence_path = (
            self.store.validation_evidence_path(
                state.task_id, evidence.validation_round
            )
            .relative_to(run_dir)
            .as_posix()
        )
        evaluation_root = run_dir / "evaluations"
        round_root = evaluation_root / f"round-{evidence.validation_round:02d}"
        aggregate_path = round_root / "aggregate.json"
        if aggregate_path.is_file():
            aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
            if not isinstance(aggregate, Mapping):
                raise InfrastructureError(
                    "non-passing validation aggregate must contain one object"
                )
            verify_control_aggregate(
                aggregate,
                evidence=evidence,
                evidence_path=evidence_path,
            )
        else:
            aggregate = build_non_passing_aggregate(
                evidence,
                evidence_path=evidence_path,
            )
            _atomic_write_json(aggregate_path, aggregate)
        _atomic_write_json(evaluation_root / "aggregate.json", aggregate)
        for filename in ("spec.json", "architecture.json"):
            (evaluation_root / filename).unlink(missing_ok=True)

    def _verify_final_validation_artifacts(
        self,
        state: RunState,
        audit: AuditRecorder,
    ) -> None:
        if not state.rounds:
            if self._validation_evidence_required(state.task_id):
                raise InfrastructureError("final run has no required validation round")
            return
        latest = state.rounds[-1]
        path = self.store.validation_evidence_path(state.task_id, latest.round_number)
        if not path.is_file():
            if self._validation_evidence_required(state.task_id):
                raise InfrastructureError(
                    "final run is missing required validation evidence"
                )
            return
        evidence = self.store.load_validation_evidence(
            state.task_id, latest.round_number
        )
        run_dir = self.store.run_dir(state.task_id)
        evidence_path = (
            self.store.validation_evidence_path(state.task_id, latest.round_number)
            .relative_to(run_dir)
            .as_posix()
        )
        round_root = run_dir / "evaluations" / f"round-{latest.round_number:02d}"
        aggregate_path = round_root / "aggregate.json"
        if evidence.status == "legacy_evidence_unavailable":
            if self._validation_evidence_required(state.task_id):
                raise InfrastructureError(
                    "required validation evidence cannot be a legacy marker"
                )
            evidence.verify_binding(
                task_id=state.task_id,
                validation_round=latest.round_number,
                base_commit=state.base_commit,
                final_diff_sha256=state.last_diff_sha256,
            )
            aggregate = self._load_final_aggregate_with_alias(
                run_dir,
                aggregate_path,
            )
            verify_control_aggregate(
                aggregate,
                evidence=evidence,
                evidence_path=evidence_path,
            )
            return
        self._verify_validation_evidence(
            state,
            audit,
            latest,
            evidence,
            expected_diff_sha256=state.last_diff_sha256,
        )
        if evidence.status != "pass":
            aggregate = self._load_final_aggregate_with_alias(
                run_dir,
                aggregate_path,
            )
            verify_control_aggregate(
                aggregate,
                evidence=evidence,
                evidence_path=evidence_path,
            )
            return
        evaluation_context_path = run_dir / "context" / "evaluation.json"
        if not aggregate_path.is_file():
            if (
                self.evaluation_coordinator is not None
                or evaluation_context_path.is_file()
            ):
                raise InfrastructureError(
                    "final evaluated run is missing its round aggregate"
                )
            return
        context = self._load_context(evaluation_context_path)
        if context is None:
            raise InfrastructureError(
                "final evaluation aggregate has no frozen evaluation context"
            )
        binding, _normalized = EvaluationCoordinator.input_binding(
            task=self.store.load_task(state.task_id),
            context=context,
            changed_files=audit.current_changed_files(),
            diff_text=audit.current_diff_text(),
            validation_evidence=evidence,
        )
        aggregate = self._load_final_aggregate_with_alias(
            run_dir,
            aggregate_path,
        )
        verify_aggregate_binding(
            aggregate,
            binding=binding,
            evidence_path=evidence_path,
            evidence_ids=[item.evidence_id for item in evidence.commands],
        )
        role_artifacts: dict[str, Mapping[str, Any]] = {}
        for filename in ("spec.json", "architecture.json"):
            path = round_root / filename
            if not path.is_file():
                raise InfrastructureError(f"final evaluated run is missing {filename}")
            artifact = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(artifact, Mapping):
                raise InfrastructureError(
                    f"final evaluation artifact must be an object: {filename}"
                )
            verify_evaluation_artifact_binding(
                artifact,
                binding=binding,
                evidence_path=evidence_path,
            )
            role_artifacts[filename] = artifact
        EvaluationCoordinator.verify_frozen_evaluation(
            task=self.store.load_task(state.task_id),
            context=context,
            changed_files=audit.current_changed_files(),
            validation_evidence=evidence,
            validation_evidence_path=evidence_path,
            binding=binding,
            spec_artifact=role_artifacts["spec.json"],
            architecture_artifact=role_artifacts["architecture.json"],
            aggregate=aggregate,
        )

    @staticmethod
    def _load_final_aggregate_with_alias(
        run_dir: Path,
        aggregate_path: Path,
    ) -> Mapping[str, Any]:
        if not aggregate_path.is_file():
            raise InfrastructureError("final run is missing its round aggregate")
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        if not isinstance(aggregate, Mapping):
            raise InfrastructureError("evaluation aggregate must contain one object")
        alias_path = run_dir / "evaluations" / "aggregate.json"
        if not alias_path.is_file():
            raise InfrastructureError("final run is missing its aggregate alias")
        alias = json.loads(alias_path.read_text(encoding="utf-8"))
        if not isinstance(alias, Mapping) or dict(alias) != dict(aggregate):
            raise InfrastructureError(
                "final evaluation aggregate alias is stale or changed"
            )
        return aggregate

    def _safe_git_summary(self, root: Path) -> str:
        try:
            status = self._run_git(root, "status", "--short", "--untracked-files=all")
            unstaged = self._run_git(root, "diff", "--stat")
            staged = self._run_git(root, "diff", "--cached", "--stat")
            return "\n\n".join(
                [
                    "git status --short:\n" + (status.strip() or "（工作区干净）"),
                    "git diff --stat:\n" + (unstaged.strip() or "（无未暂存差异）"),
                    "git diff --cached --stat:\n"
                    + (staged.strip() or "（无已暂存差异）"),
                ]
            )
        except InfrastructureError as exc:
            return f"无法读取最终 Git 摘要：{redact_sensitive_text(str(exc))}"

    @staticmethod
    def _run_git(root: Path, *arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise InfrastructureError(
                f"Unable to run git ({type(exc).__name__})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError("Git inspection timed out") from exc
        if completed.returncode != 0:
            detail = redact_sensitive_text(
                (completed.stderr or completed.stdout).strip()
            )[:1_000]
            raise InfrastructureError(
                f"Git inspection failed with exit code {completed.returncode}: {detail}"
            )
        return completed.stdout


__all__ = [
    "MAX_CODEX_TURNS",
    "MAX_VALIDATION_FAILURES",
    "ActiveRunError",
    "OrchestrationWorkflow",
]
