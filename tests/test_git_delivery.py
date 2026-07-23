from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from codex_loop.audit import AuditRecorder
from codex_loop.archiver import (
    ArchiveCandidate,
    ArchiveCoordinator,
    ArchiveRoleOutput,
)
from codex_loop.context import ContextSnapshot
from codex_loop.git_delivery import DeliveryError, GitDeliveryService
from codex_loop.mcp_client import McpCallError
from codex_loop.memory import MediumTermMemory
from codex_loop.models import (
    DeliveryStatus,
    RunResult,
    RunStatus,
    TaskSpec,
)
from codex_loop.review import ReviewService
from codex_loop.state import StateStore
from codex_loop.workspace import WorkspaceManager


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
    git(tmp_path, "config", "user.name", "Harness Test")
    git(tmp_path, "config", "user.email", "harness@example.com")
    (tmp_path / ".gitignore").write_text(".codex-orchestrator/\n", encoding="utf-8")
    (tmp_path / "one.txt").write_text("base one\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("base two\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


def prepare_approved_run(
    repository: Path,
    *,
    task_id: str,
    changes: dict[str, str],
    queue_id: str | None = None,
    sequence: int | None = None,
    inherited_diff: Path | None = None,
    inherited_sha256: str = "",
) -> tuple[StateStore, Path, str]:
    task = TaskSpec(
        task_id=task_id,
        requirement=f"Deliver {task_id}",
        acceptance_criteria=["The approved files are committed"],
        queue_id=queue_id,
        sequence=sequence,
    )
    base_commit = git(repository, "rev-parse", "main")
    manager = WorkspaceManager(repository, base_ref=base_commit)
    workspace = manager.create(task)
    if inherited_diff is not None:
        manager.apply_inherited_diff(workspace, inherited_diff, inherited_sha256)
    runs_root = (
        repository / ".codex-orchestrator" / "runs"
        if queue_id is None
        else repository / ".codex-orchestrator" / "queues" / queue_id / "subtasks"
    )
    store = StateStore(repository, runs_root=runs_root)
    state = store.initialize_run(
        task,
        task_repo_root=workspace.worktree,
        workspace={
            "base_ref": workspace.base_ref,
            "base_commit": workspace.base_commit,
            "task_branch": workspace.task_branch,
            "worktree_relative_path": workspace.worktree_relative_path,
            "source_worktree_was_dirty": workspace.source_worktree_was_dirty,
            "inherited_baseline": inherited_diff is not None,
            "inherited_diff_sha256": inherited_sha256,
        },
    )
    store.save_manifest(task_id, workspace.manifest())
    for relative, content in changes.items():
        target = workspace.worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    audit = AuditRecorder(
        store.run_dir(task_id),
        workspace.worktree,
        workspace.base_commit,
        inherited_baseline=inherited_diff is not None,
        queue_task=queue_id is not None,
    )
    captured = audit.capture_final_changes()
    sha = str(captured["final_diff"]["raw_sha256"])
    state.last_diff_sha256 = sha
    state.mark_success()
    store.save_state(state)
    store.save_result(RunResult.from_run(task, state))
    ReviewService(repository, store=store).record(
        task_id,
        decision="approved",
        reviewer="Local Reviewer",
        comment="Diff and tests checked.",
        reviewed_diff_sha256=sha,
        commit_subject=f"deliver {task_id}",
    )
    return store, workspace.worktree, sha


def test_delivery_creates_one_review_bound_commit_and_is_idempotent(
    repository: Path,
) -> None:
    store, worktree, reviewed_sha = prepare_approved_run(
        repository,
        task_id="delivery-single",
        changes={"one.txt": "approved one\n"},
    )
    service = GitDeliveryService(repository, store=store)

    first = service.deliver("delivery-single")
    second = service.deliver("delivery-single")

    assert first["commit_sha"] == second["commit_sha"]
    assert first["reviewed_diff_sha256"] == reviewed_sha
    assert first["subject"] == "deliver delivery-single"
    assert git(worktree, "rev-list", "--count", f"{first['parent']}..HEAD") == "1"
    assert git(worktree, "status", "--porcelain") == ""
    assert git(repository, "rev-parse", "main") == first["parent"]
    assert (
        store.load_state("delivery-single").delivery_status is DeliveryStatus.COMMITTED
    )


def test_delivery_accepts_exact_file_blocks_when_new_file_order_differs(
    repository: Path,
) -> None:
    store, worktree, reviewed_sha = prepare_approved_run(
        repository,
        task_id="delivery-mixed-order",
        changes={
            "two.txt": "approved two\n",
            "aaa-new.txt": "approved new\n",
        },
    )
    saved_diff = (
        store.run_dir("delivery-mixed-order") / "changes" / "final.diff"
    ).read_bytes()

    record = GitDeliveryService(repository, store=store).deliver("delivery-mixed-order")

    committed_diff = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "show",
            "--format=",
            "--binary",
            "--find-renames",
            "HEAD",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert hashlib.sha256(saved_diff).hexdigest() == reviewed_sha
    assert saved_diff != committed_diff
    assert git(worktree, "show", "HEAD:aaa-new.txt") == "approved new"
    assert git(worktree, "show", "HEAD:two.txt") == "approved two"
    assert record["staged_diff_sha256"] == hashlib.sha256(committed_diff).hexdigest()


def test_failed_delivery_recovers_exact_pre_staged_content_with_legacy_order(
    repository: Path,
) -> None:
    store, worktree, _sha = prepare_approved_run(
        repository,
        task_id="delivery-failed-staged-order",
        changes={
            "two.txt": "approved two\n",
            "aaa-new.txt": "approved new\n",
        },
    )
    saved_diff = (
        store.run_dir("delivery-failed-staged-order") / "changes" / "final.diff"
    ).read_bytes()
    git(worktree, "add", "--all", "--", "aaa-new.txt", "two.txt")
    staged_diff = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "diff",
            "--cached",
            "--binary",
            "--find-renames",
            "HEAD",
            "--",
        ],
        check=True,
        capture_output=True,
    ).stdout
    state = store.load_state("delivery-failed-staged-order")
    state.delivery_status = DeliveryStatus.FAILED
    store.save_state(state)

    assert saved_diff != staged_diff
    recovered = GitDeliveryService(repository, store=store).deliver(
        "delivery-failed-staged-order"
    )

    assert recovered["status"] == "committed"
    assert recovered["staged_diff_sha256"] == hashlib.sha256(staged_diff).hexdigest()
    assert git(worktree, "status", "--porcelain") == ""


def test_delivery_rejects_different_pre_staged_file_content(
    repository: Path,
) -> None:
    store, worktree, _sha = prepare_approved_run(
        repository,
        task_id="delivery-staged-tamper",
        changes={"one.txt": "approved one\n"},
    )
    (worktree / "one.txt").write_text("tampered one\n", encoding="utf-8")
    git(worktree, "add", "one.txt")
    (worktree / "one.txt").write_text("approved one\n", encoding="utf-8")

    with pytest.raises(DeliveryError, match="unexpected pre-staged content"):
        GitDeliveryService(repository, store=store).deliver("delivery-staged-tamper")

    assert git(worktree, "rev-parse", "HEAD") == git(repository, "rev-parse", "main")
    assert (
        store.load_state("delivery-staged-tamper").delivery_status
        is DeliveryStatus.FAILED
    )


def test_delivery_rejects_post_review_tampering_without_creating_commit(
    repository: Path,
) -> None:
    store, worktree, _sha = prepare_approved_run(
        repository,
        task_id="delivery-tamper",
        changes={"one.txt": "approved one\n"},
    )
    base = git(worktree, "rev-parse", "HEAD")
    (worktree / "extra.txt").write_text("not reviewed\n", encoding="utf-8")

    with pytest.raises(DeliveryError, match="changed after human review"):
        GitDeliveryService(repository, store=store).deliver("delivery-tamper")

    assert git(worktree, "rev-parse", "HEAD") == base
    assert store.load_state("delivery-tamper").delivery_status is DeliveryStatus.FAILED


def test_unknown_head_becomes_infrastructure_error_without_reset(
    repository: Path,
) -> None:
    store, worktree, _sha = prepare_approved_run(
        repository,
        task_id="delivery-unknown-head",
        changes={"one.txt": "approved one\n"},
    )
    (worktree / "unknown.txt").write_text("unknown\n", encoding="utf-8")
    git(worktree, "add", "unknown.txt")
    git(worktree, "commit", "-qm", "unknown commit")
    unknown_head = git(worktree, "rev-parse", "HEAD")

    with pytest.raises(DeliveryError, match="HEAD changed"):
        GitDeliveryService(repository, store=store).deliver("delivery-unknown-head")

    state = store.load_state("delivery-unknown-head")
    assert git(worktree, "rev-parse", "HEAD") == unknown_head
    assert state.status is RunStatus.INFRASTRUCTURE_ERROR
    assert state.delivery_status is DeliveryStatus.FAILED


class CrashAfterGitCommit(GitDeliveryService):
    def _recover_created_commit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if not getattr(self, "crashed", False):
            self.crashed = True
            raise RuntimeError("injected crash after git commit")
        return super()._recover_created_commit(*args, **kwargs)


class InjectedProcessExit(BaseException):
    pass


class CrashAfterStaging(GitDeliveryService):
    def _stage_reviewed_files(self, worktree: Path, reviewed_files: list[str]) -> None:
        super()._stage_reviewed_files(worktree, reviewed_files)
        raise InjectedProcessExit("injected exit after staging")


def test_staged_content_is_recovered_when_process_exits_before_intent(
    repository: Path,
) -> None:
    store, worktree, _sha = prepare_approved_run(
        repository,
        task_id="delivery-staging-recover",
        changes={"one.txt": "approved staged content\n"},
    )
    base = git(worktree, "rev-parse", "HEAD")

    with pytest.raises(InjectedProcessExit, match="injected exit after staging"):
        CrashAfterStaging(repository, store=store).deliver("delivery-staging-recover")

    assert git(worktree, "diff", "--cached", "--name-only") == "one.txt"
    assert (
        store.load_state("delivery-staging-recover").delivery_status
        is DeliveryStatus.COMMITTING
    )

    recovered = GitDeliveryService(repository, store=store).deliver(
        "delivery-staging-recover"
    )

    assert recovered["parent"] == base
    assert git(worktree, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert git(worktree, "status", "--porcelain") == ""


def test_commit_created_before_state_write_is_recovered_without_duplicate(
    repository: Path,
) -> None:
    store, worktree, _sha = prepare_approved_run(
        repository,
        task_id="delivery-recover",
        changes={"one.txt": "approved one\n"},
    )
    base = git(worktree, "rev-parse", "HEAD")
    crashing = CrashAfterGitCommit(repository, store=store)
    with pytest.raises(RuntimeError, match="injected crash"):
        crashing.deliver("delivery-recover")
    created = git(worktree, "rev-parse", "HEAD")
    assert created != base

    recovered = GitDeliveryService(repository, store=store).deliver("delivery-recover")

    assert recovered["commit_sha"] == created
    assert recovered["recovered"] is True
    assert git(worktree, "rev-list", "--count", f"{base}..HEAD") == "1"


def test_queue_commit_is_one_cumulative_snapshot_from_original_base(
    repository: Path,
) -> None:
    first_store, first_worktree, _sha = prepare_approved_run(
        repository,
        task_id="queue-delivery-task-01",
        queue_id="queue-delivery",
        sequence=1,
        changes={"one.txt": "queue one\n"},
    )
    first_record = GitDeliveryService(repository, store=first_store).deliver(
        "queue-delivery-task-01"
    )
    first_diff = (
        first_store.run_dir("queue-delivery-task-01") / "changes" / "cumulative.diff"
    )
    inherited_sha = hashlib.sha256(first_diff.read_bytes()).hexdigest()

    second_store, second_worktree, _sha = prepare_approved_run(
        repository,
        task_id="queue-delivery-task-02",
        queue_id="queue-delivery",
        sequence=2,
        inherited_diff=first_diff,
        inherited_sha256=inherited_sha,
        changes={"two.txt": "queue two\n"},
    )
    second_record = GitDeliveryService(repository, store=second_store).deliver(
        "queue-delivery-task-02"
    )

    assert second_record["parent"] == first_record["parent"]
    assert git(second_worktree, "show", "HEAD:one.txt") == "queue one"
    assert git(second_worktree, "show", "HEAD:two.txt") == "queue two"
    assert (
        git(second_worktree, "rev-list", "--count", f"{second_record['parent']}..HEAD")
        == "1"
    )
    assert git(first_worktree, "rev-parse", "HEAD") == first_record["commit_sha"]


class ArchiveRole:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **_kwargs: Any) -> Any:
        self.calls += 1
        return type(
            "ArchiveRun",
            (),
            {
                "thread_id": "thread-archiver",
                "output": ArchiveRoleOutput(
                    outcome="no_candidate",
                    task_summary="已批准的变更已经提交并通过验证。",
                    tags=["delivery"],
                    technologies=["git"],
                    candidates=[],
                ),
            },
        )()


def test_archive_models_require_chinese_knowledge_and_english_tags() -> None:
    candidate = ArchiveCandidate(
        candidate_type="guideline",
        title="仅在提交成功后归档证据",
        scope="需要归档提交证据的编排流程",
        problem="提前归档会产生与真实仓库状态不一致的记录。",
        action="确认提交成功后，再归档提交身份和验证证据。",
        result="归档记录始终对应真实存在的提交状态。",
        target_layer="layer1",
        layer_reason="该规则属于可以跨项目复用的技术知识。",
        source_ids=["task:test"],
        tags=["commit-gating", "auditability"],
    )

    assert candidate.tags == ["commit-gating", "auditability"]

    with pytest.raises(ValueError, match="written in Chinese"):
        ArchiveCandidate(
            candidate_type="guideline",
            title="Archive evidence after commit success",
            scope=candidate.scope,
            problem=candidate.problem,
            action=candidate.action,
            result=candidate.result,
            target_layer=candidate.target_layer,
            layer_reason=candidate.layer_reason,
            source_ids=candidate.source_ids,
            tags=candidate.tags,
        )

    with pytest.raises(ValueError, match="lowercase English tokens"):
        ArchiveCandidate(
            candidate_type="guideline",
            title=candidate.title,
            scope=candidate.scope,
            problem=candidate.problem,
            action=candidate.action,
            result=candidate.result,
            target_layer=candidate.target_layer,
            layer_reason=candidate.layer_reason,
            source_ids=candidate.source_ids,
            tags=["归档"],
        )

    with pytest.raises(ValueError, match="written in Chinese"):
        ArchiveRoleOutput(
            outcome="no_candidate",
            task_summary="The approved change was archived.",
            tags=["delivery"],
            technologies=["git"],
            candidates=[],
        )

    with pytest.raises(ValueError, match="require business_domain"):
        ArchiveCandidate(
            candidate_type="guideline",
            title=candidate.title,
            scope=candidate.scope,
            problem=candidate.problem,
            action=candidate.action,
            result=candidate.result,
            target_layer="layer2",
            layer_reason="该规则属于跨项目复用的业务知识。",
            source_ids=candidate.source_ids,
            tags=candidate.tags,
        )


class ArchiveClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.fail:
            raise McpCallError("temporary fixture write failure")
        return {"status": "ok", "tool": name}


class LayeredArchiveRole(ArchiveRole):
    def run(self, **_kwargs: Any) -> Any:
        self.calls += 1
        return type(
            "ArchiveRun",
            (),
            {
                "thread_id": "thread-layered-archiver",
                "output": ArchiveRoleOutput(
                    outcome="candidates",
                    task_summary="已提炼一条可以跨项目复用的技术知识。",
                    tags=["archive"],
                    technologies=["git"],
                    candidates=[
                        ArchiveCandidate(
                            candidate_type="guideline",
                            title="仅在提交成功后归档证据",
                            scope="需要归档提交证据的编排流程",
                            problem="提前归档会产生与真实状态不一致的记录。",
                            action="确认提交成功后再归档提交身份和验证证据。",
                            result="归档记录始终对应真实存在的提交状态。",
                            target_layer="layer1",
                            layer_reason="该规则可以被不同项目的编排流程复用。",
                            source_ids=["task:archive-layered"],
                            tags=["commit-gating"],
                        )
                    ],
                ),
            },
        )()


def archive_coordinator(
    repository: Path,
    role: ArchiveRole,
    client: ArchiveClient,
) -> ArchiveCoordinator:
    registry_path = repository / "fixture-mcp-registry.json"
    registry_path.write_text(
        json.dumps({"knowledge": {"business_domains": []}}),
        encoding="utf-8",
    )
    return ArchiveCoordinator(
        repository,
        project_id="accounting",
        consumer_actor="zhangsan",
        writer_actor="orchestrator",
        role_runner=role,  # type: ignore[arg-type]
        memory=MediumTermMemory(repository),
        archive_client=client,  # type: ignore[arg-type]
        registry_path=registry_path,
    )


def test_archiver_persists_local_summary_before_idempotent_outbox(
    repository: Path,
) -> None:
    store, _worktree, _sha = prepare_approved_run(
        repository,
        task_id="archive-success",
        changes={"one.txt": "archived one\n"},
    )
    GitDeliveryService(repository, store=store).deliver("archive-success")
    role = ArchiveRole()
    client = ArchiveClient()
    coordinator = archive_coordinator(repository, role, client)

    completed = coordinator.archive(
        "archive-success",
        store=store,
        archive_context=ContextSnapshot(
            stage="archive",
            query="archive",
            actor="zhangsan",
        ),
    )

    run_dir = store.run_dir("archive-success")
    packet = (run_dir / "archive" / "packet.json").read_text(encoding="utf-8")
    assert completed["status"] == "completed"
    assert (
        store.load_state("archive-success").delivery_status is DeliveryStatus.ARCHIVED
    )
    assert role.calls == 1
    assert [name for name, _arguments in client.calls] == [
        "knowledge_workflow_complete"
    ]
    assert "full command logs" in packet
    assert "diff --git" not in packet
    assert (
        repository / ".codex-orchestrator/memory/run_summaries/archive-success.json"
    ).is_file()
    calls_before_retry = len(client.calls)
    coordinator.archive(
        "archive-success",
        store=store,
        archive_context=ContextSnapshot(
            stage="archive",
            query="archive",
            actor="zhangsan",
        ),
    )
    assert len(client.calls) == calls_before_retry
    assert role.calls == 1


def test_archive_packet_preserves_evaluation_binding_without_command_logs(
    repository: Path,
) -> None:
    store, _worktree, _sha = prepare_approved_run(
        repository,
        task_id="archive-evaluation-binding",
        changes={"one.txt": "bound archive\n"},
    )
    run_dir = store.run_dir("archive-evaluation-binding")
    evaluation = {
        "schema_version": 2,
        "validation_round": 1,
        "validation": {
            "status": "pass",
            "evidence_ids": ["VAL-001", "VAL-002"],
            "evidence_path": "validation/evidence-round-01.json",
        },
        "validation_evidence_sha256": "a" * 64,
        "final_diff_sha256": "b" * 64,
        "context_sha256": "c" * 64,
        "changed_files_sha256": "d" * 64,
        "evaluation_input_sha256": "e" * 64,
        "requires_repair": False,
        "requires_human": False,
    }
    evaluation_path = run_dir / "evaluations/aggregate.json"
    evaluation_path.parent.mkdir(parents=True)
    evaluation_path.write_text(
        json.dumps(evaluation, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    GitDeliveryService(repository, store=store).deliver("archive-evaluation-binding")
    role = ArchiveRole()
    coordinator = archive_coordinator(
        repository,
        role,
        ArchiveClient(),
    )

    coordinator.archive(
        "archive-evaluation-binding",
        store=store,
        archive_context=ContextSnapshot(
            stage="archive",
            query="archive",
            actor="zhangsan",
        ),
    )

    packet_path = run_dir / "archive/packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["evaluation"] == evaluation
    packet_text = packet_path.read_text(encoding="utf-8")
    assert "stdout" not in packet_text
    assert "stderr" not in packet_text
    assert "full command logs" in packet_text
    first_packet = packet_text
    coordinator.archive(
        "archive-evaluation-binding",
        store=store,
        archive_context=ContextSnapshot(
            stage="archive",
            query="archive",
            actor="zhangsan",
        ),
    )
    assert packet_path.read_text(encoding="utf-8") == first_packet
    assert role.calls == 1


def test_archiver_routes_candidates_without_preallocating_knowledge_ids(
    repository: Path,
) -> None:
    store, _worktree, _sha = prepare_approved_run(
        repository,
        task_id="archive-layered",
        changes={"one.txt": "layered archive\n"},
    )
    GitDeliveryService(repository, store=store).deliver("archive-layered")
    role = LayeredArchiveRole()
    client = ArchiveClient()

    completed = archive_coordinator(repository, role, client).archive(
        "archive-layered",
        store=store,
        archive_context=ContextSnapshot(
            stage="archive",
            query="archive",
            actor="zhangsan",
        ),
    )

    assert completed["status"] == "completed"
    name, arguments = client.calls[0]
    assert name == "knowledge_create_draft"
    assert arguments["target_layer"] == "layer1"
    assert arguments["project_id"] == "accounting"
    assert arguments["business_domain"] == ""
    assert "knowledge_id" not in arguments
    assert "## 分层依据" in arguments["content"]
    assert [name for name, _arguments in client.calls] == [
        "knowledge_create_draft",
        "knowledge_workflow_complete",
    ]


def test_archive_write_failure_does_not_rollback_commit_and_retries_only_outbox(
    repository: Path,
) -> None:
    store, worktree, _sha = prepare_approved_run(
        repository,
        task_id="archive-retry",
        changes={"one.txt": "retry archive\n"},
    )
    commit = GitDeliveryService(repository, store=store).deliver("archive-retry")
    role = ArchiveRole()
    client = ArchiveClient(fail=True)
    coordinator = archive_coordinator(repository, role, client)

    failed = coordinator.archive(
        "archive-retry",
        store=store,
        archive_context=ContextSnapshot(
            stage="archive",
            query="archive",
            actor="zhangsan",
        ),
    )

    assert failed["status"] == "failed"
    assert store.load_state("archive-retry").delivery_status is DeliveryStatus.FAILED
    assert git(worktree, "rev-parse", "HEAD") == commit["commit_sha"]
    assert role.calls == 1
    client.fail = False
    completed = coordinator.retry("archive-retry", store=store)
    assert completed["status"] == "completed"
    assert store.load_state("archive-retry").delivery_status is DeliveryStatus.ARCHIVED
    assert role.calls == 1
