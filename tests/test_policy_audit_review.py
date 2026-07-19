from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from codex_loop.audit import AuditRecorder, file_sha256
from codex_loop.models import InfrastructureError, ReviewStatus, TaskSpec
from codex_loop.policy import ExecutionPolicy
from codex_loop.report import ReportBuilder
from codex_loop.review import ReviewError, ReviewService
from codex_loop.state import StateStore
from codex_loop.workspace import WorkspaceInfo, WorkspaceManager


def git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
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
    (tmp_path / ".gitignore").write_text(
        ".codex-orchestrator/\n"
        ".codex-runtime/\n"
        "node_modules/\n"
        "frontend/node_modules/\n",
        encoding="utf-8",
    )
    (tmp_path / "modified.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    (tmp_path / "renamed.txt").write_text("rename me unchanged\n", encoding="utf-8")
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


def task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        requirement="Exercise isolation",
        acceptance_criteria=["Artifacts are complete"],
    )


def workspace(repository: Path, task_id: str) -> WorkspaceInfo:
    return WorkspaceManager(repository).create(task(task_id))


def valid_effective_config(repository: Path) -> dict[str, object]:
    return {
        "approval_policy": "never",
        "default_permissions": "loop-harness",
        "sandbox_mode": None,
        "web_search": "disabled",
        "permissions": {
            "loop-harness": {
                "extends": ":read-only",
                "filesystem": {":root": "write"},
                "network": {"enabled": True},
            }
        },
    }


def test_policy_removes_credentials_and_rejects_wider_effective_access(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = workspace(repository, "policy-test")
    policy = ExecutionPolicy(repository, info)
    monkeypatch.setenv("SERVICE_TOKEN", "do-not-inherit")
    monkeypatch.setenv("DATABASE_URL", "postgresql://production")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    policy.prepare_runtime()

    environment = policy.environment()
    validation_environment = policy.validation_environment()
    requested = policy.requested_snapshot()
    effective = policy.verify_effective(valid_effective_config(repository))

    assert "SERVICE_TOKEN" not in environment
    assert "DATABASE_URL" not in environment
    assert Path(environment["HOME"]).is_relative_to(info.worktree)
    assert Path(environment["CODEX_HOME"]).is_relative_to(info.worktree)
    assert "CODEX_HOME" not in validation_environment
    if Path("/Library/Developer/CommandLineTools").is_dir():
        assert environment["DEVELOPER_DIR"] == (
            "/Library/Developer/CommandLineTools"
        )
        assert environment["PATH"].split(os.pathsep)[0] == (
            "/Library/Developer/CommandLineTools/usr/bin"
        )
    assert requested["requested"]["network"] == "disabled"
    assert effective["effective"]["verified"] is True
    assert "features.multi_agent=false" in policy.config_overrides()
    assert ExecutionPolicy.denied_command_reason(["git", "commit", "-m", "x"])
    assert ExecutionPolicy.denied_command_reason(["kubectl", "apply", "-f", "x"])
    assert ExecutionPolicy.denied_command_classification(
        "/bin/zsh -lc 'curl -I https://example.com'"
    ) == ("network", "network command is forbidden")
    assert ExecutionPolicy.denied_command_classification(
        "/bin/zsh -lc 'git commit --allow-empty -m blocked'"
    ) == ("git", "Git branch, commit, and history mutations are forbidden")

    wider = valid_effective_config(repository)
    wider["permissions"]["loop-harness"]["network"]["enabled"] = False  # type: ignore[index]
    with pytest.raises(InfrastructureError, match="external-sandbox profile is unknown"):
        policy.verify_effective(wider)

    wider_approval = valid_effective_config(repository)
    wider_approval["approval_policy"] = "on-request"
    with pytest.raises(InfrastructureError, match="approval policy is wider"):
        policy.verify_effective(wider_approval)


def test_policy_runtime_copies_only_login_material_into_task_home(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_home = repository.parent / f"{repository.name}-codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text(
        '{"token":"control-plane-only"}\n', encoding="utf-8"
    )
    (source_home / "config.toml").write_text(
        'approval_policy = "on-request"\n', encoding="utf-8"
    )
    dependency_entries = (
        repository / "node_modules/@openai",
        repository / "node_modules/.tmp",
        repository / "frontend/node_modules/.bin",
        repository / "frontend/node_modules/vitest",
    )
    for entry in dependency_entries:
        entry.mkdir(parents=True)
    runtime_root = repository.parent / f"{repository.name}-node-runtime"
    runtime_bin = runtime_root / "bin"
    runtime_bin.mkdir(parents=True)
    for executable in ("node", "npm", "kubectl"):
        path = runtime_bin / executable
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("PATH", f"{runtime_bin}:{os.environ.get('PATH', '')}")
    info = workspace(repository, "runtime-home-test")
    policy = ExecutionPolicy(repository, info)

    policy.prepare_runtime()
    policy.stage_app_server_executable("/usr/bin/true")

    copied_auth = policy.codex_home_dir / "auth.json"
    assert copied_auth.is_file()
    assert copied_auth.read_text(encoding="utf-8") == (
        source_home / "auth.json"
    ).read_text(encoding="utf-8")
    assert stat.S_IMODE(copied_auth.stat().st_mode) == 0o600
    assert not (policy.codex_home_dir / "config.toml").exists()
    serialized_overrides = " ".join(policy.config_overrides())
    assert 'default_permissions="loop-harness"' in serialized_overrides
    assert '":root"="write"' in serialized_overrides
    assert '"network"={"enabled"=true}' in serialized_overrides
    if Path("/usr/bin/sandbox-exec").is_file():
        app_server_profile = policy.app_server_profile_path.read_text(
            encoding="utf-8"
        )
        assert "(deny default)" in app_server_profile
        assert "(allow network*)" in app_server_profile
        assert (
            f'(process-path "{policy.app_server_executable_path}")'
            in app_server_profile
        )
        assert (
            f'(deny file-write* (literal "{policy.app_server_profile_path}"))'
            in app_server_profile
        )
        worktree_git_file = info.worktree / ".git"
        assert (
            f'(deny file-write* (literal "{worktree_git_file}"))'
            in app_server_profile
        )
        blocked_target = repository / "app-server-outside.txt"
        blocked = subprocess.run(
            [
                *policy.app_server_command_prefix(),
                "/bin/zsh",
                "-lc",
                f"printf blocked > {blocked_target}",
            ],
            cwd=info.worktree,
            env=policy.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert blocked.returncode != 0
        assert not blocked_target.exists()

        allowed_target = info.worktree / "app-server-inside.txt"
        allowed = subprocess.run(
            [
                *policy.app_server_command_prefix(),
                "/bin/zsh",
                "-lc",
                f"printf allowed > {allowed_target}",
            ],
            cwd=info.worktree,
            env=policy.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert allowed.returncode == 0
        assert allowed_target.read_text(encoding="utf-8") == "allowed"
        allowed_target.unlink()

        blocked_auth = subprocess.run(
            [
                *policy.app_server_command_prefix(),
                "/usr/bin/head",
                "-c",
                "1",
                str(copied_auth),
            ],
            cwd=info.worktree,
            env=policy.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert blocked_auth.returncode != 0

        blocked_network = subprocess.run(
            [
                *policy.app_server_command_prefix(),
                "/usr/bin/curl",
                "-I",
                "--max-time",
                "2",
                "https://example.com",
            ],
            cwd=info.worktree,
            env=policy.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert blocked_network.returncode != 0

        blocked_production = subprocess.run(
            [
                *policy.app_server_command_prefix(),
                str(runtime_bin / "kubectl"),
            ],
            cwd=info.worktree,
            env=policy.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert blocked_production.returncode != 0

        policy.retire_app_server_material()
        assert not copied_auth.exists()
        assert not policy.app_server_executable_path.exists()
    for relative in (
        Path("node_modules"),
        Path("frontend/node_modules"),
    ):
        dependency_root = info.worktree / relative
        assert dependency_root.is_dir()
        assert not dependency_root.is_symlink()
        assert any(entry.is_symlink() for entry in dependency_root.iterdir())
        for cache_name in (".cache", ".tmp", ".vite", ".vite-temp"):
            cache_path = dependency_root / cache_name
            assert cache_path.is_dir()
            assert not cache_path.is_symlink()
    assert git(info.worktree, "status", "--porcelain", "--untracked-files=all") == ""
    if Path("/usr/bin/sandbox-exec").is_file():
        policy.validation_command_prefix()
        profile = (policy.runtime_root / "validation.sb").read_text(encoding="utf-8")
        assert f'(deny file-read* (literal "{copied_auth}"))' in profile
        assert runtime_root.as_posix() in profile
        assert "(allow signal (target same-sandbox))" in profile


def test_policy_maps_only_project_configured_dependency_paths(
    repository: Path,
) -> None:
    gitignore = repository / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "web/ui/node_modules/\n",
        encoding="utf-8",
    )
    git(repository, "add", ".gitignore")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "configure custom dependencies",
    )
    source_package = repository / "web/ui/node_modules/example"
    source_package.mkdir(parents=True)
    (source_package / "index.js").write_text("export default true;\n", encoding="utf-8")
    info = workspace(repository, "custom-dependency-test")
    policy = ExecutionPolicy(
        repository,
        info,
        dependency_paths=(Path("web/ui/node_modules"),),
    )

    policy.prepare_runtime()

    mapped = info.worktree / "web/ui/node_modules/example"
    assert mapped.is_symlink()
    assert mapped.resolve() == source_package.resolve()
    assert not (info.worktree / "frontend/node_modules").exists()
    assert git(info.worktree, "status", "--porcelain", "--untracked-files=all") == ""


def test_policy_allows_only_package_manifests_needed_by_shared_node_dependencies(
    repository: Path,
) -> None:
    info = workspace(repository, "package-scope-policy-test")
    scope_manifest = repository / "package.json"
    scope_manifest.write_text('{"private":true}\n', encoding="utf-8")
    policy = ExecutionPolicy(repository, info)

    policy.prepare_runtime()
    expected = f'(allow file-read* (literal "{scope_manifest}"))'
    app_server_profile = policy.app_server_profile_path.read_text(encoding="utf-8")

    assert expected in app_server_profile
    assert f'(allow file-read* (subpath "{repository.parent}"))' not in app_server_profile

    if Path("/usr/bin/sandbox-exec").is_file():
        policy.validation_command_prefix()
        validation_profile = (policy.runtime_root / "validation.sb").read_text(
            encoding="utf-8"
        )
        assert expected in validation_profile
        assert (
            f'(allow file-read* (subpath "{repository.parent}"))'
            not in validation_profile
        )


def test_audit_captures_complete_diff_hashes_events_and_secret_redaction(
    repository: Path,
) -> None:
    info = workspace(repository, "audit-test")
    run_dir = repository / ".codex-orchestrator/runs/audit-test"
    audit = AuditRecorder(run_dir, info.worktree, info.base_commit)
    (info.worktree / "modified.txt").write_text("new\n", encoding="utf-8")
    (info.worktree / "deleted.txt").unlink()
    git(info.worktree, "mv", "renamed.txt", "moved.txt")
    (info.worktree / "added.txt").write_text(
        "token=sk-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8"
    )
    (info.worktree / "binary.bin").write_bytes(b"\x00\x01\xff")

    audit.append("run.created", {"task_id": "audit-test"})
    audit.save_prompt(1, "the exact prompt")
    audit.save_response(1, "visible response")
    audit.record_codex_notification(
        1,
        SimpleNamespace(method="new/item", payload={"future": True}),
    )
    audit.record_codex_notification(
        1,
        SimpleNamespace(
            method="item/completed",
            payload={
                "item": {
                    "type": "reasoning",
                    "content": ["hidden reasoning must not be stored"],
                    "summary": ["hidden summary"],
                }
            },
        ),
    )
    audit.record_codex_notification(
        1,
        SimpleNamespace(
            method="item/reasoning/textDelta",
            payload={"delta": "hidden reasoning delta must not be stored"},
        ),
    )
    audit.record_codex_notification(
        1,
        SimpleNamespace(
            method="item/reasoning/summaryTextDelta",
            payload={"delta": "hidden reasoning summary must not be stored"},
        ),
    )
    audit.record_codex_notification(
        1,
        SimpleNamespace(
            method="item/completed",
            payload={
                "item": {
                    "type": "commandExecution",
                    "command": "/bin/zsh -lc 'curl -I https://example.com'",
                    "commandActions": [
                        {"command": "curl -I https://example.com"}
                    ],
                    "status": "failed",
                    "exitCode": 6,
                    "aggregatedOutput": "Could not resolve host",
                }
            },
        ),
    )
    outside_target = repository / "modified.txt"
    audit.record_codex_notification(
        1,
        SimpleNamespace(
            method="item/completed",
            payload={
                "item": {
                    "type": "commandExecution",
                    "command": (
                        f"/bin/zsh -lc 'head -c 1 {outside_target.as_posix()}'"
                    ),
                    "status": "failed",
                    "exitCode": 1,
                    "aggregatedOutput": "Operation not permitted",
                }
            },
        ),
    )
    changes = audit.capture_final_changes()

    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    paths = {item["path"]: item for item in changes["files"]}
    final_diff = changes["final_diff"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert any(event["type"] == "codex.item.unknown" for event in events)
    assert any(event["type"] == "permission.denied" for event in events)
    assert any(
        event["type"] == "permission.denied"
        and event["payload"].get("category") == "filesystem"
        and event["payload"].get("target") == outside_target.as_posix()
        for event in events
    )
    assert "hidden reasoning" not in (run_dir / "events.jsonl").read_text()
    assert (run_dir / "turns/turn-01/prompt.md").read_text() == "the exact prompt"
    assert (run_dir / "turns/turn-01/response.md").read_text() == "visible response"
    assert paths["modified.txt"]["status"] == "modified"
    assert paths["deleted.txt"]["status"] == "deleted"
    assert paths["added.txt"]["status"] == "added"
    assert paths["binary.bin"]["after_sha256"]
    assert final_diff["raw_sha256"] == audit.current_diff_sha256()
    assert final_diff["stored_sha256"] == file_sha256(run_dir / "changes/final.diff")
    assert final_diff["redaction_count"] > 0
    stored = (run_dir / "changes/final.diff").read_text()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in stored
    assert "[REDACTED]" in stored


def test_audit_fails_closed_if_an_outside_command_succeeds(
    repository: Path,
) -> None:
    info = workspace(repository, "outside-command-test")
    audit = AuditRecorder(
        repository / ".codex-orchestrator/runs/outside-command-test",
        info.worktree,
        info.base_commit,
    )
    outside_target = repository / "modified.txt"

    with pytest.raises(InfrastructureError, match="forbidden.*unexpectedly succeeded"):
        audit.record_codex_notification(
            1,
            SimpleNamespace(
                method="item/completed",
                payload={
                    "item": {
                        "type": "commandExecution",
                        "command": ["head", "-c", "1", str(outside_target)],
                        "status": "completed",
                        "exitCode": 0,
                        "aggregatedOutput": "o",
                    }
                },
            ),
        )

    events = [
        json.loads(line)
        for line in audit.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["type"] == "permission.denied"
    assert events[-1]["payload"]["target"] == outside_target.as_posix()


def test_audit_records_a_denied_helper_write_without_claiming_sandbox_escape(
    repository: Path,
) -> None:
    info = workspace(repository, "helper-denial-test")
    audit = AuditRecorder(
        repository / ".codex-orchestrator/runs/helper-denial-test",
        info.worktree,
        info.base_commit,
    )

    audit.record_codex_notification(
        1,
        SimpleNamespace(
            method="item/completed",
            payload={
                "item": {
                    "type": "commandExecution",
                    "command": ["git", "status", "--short"],
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": (
                        "could not create helper cache: Operation not permitted"
                    ),
                }
            },
        ),
    )

    events = [
        json.loads(line)
        for line in audit.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["type"] == "permission.denied"
    assert events[-1]["payload"]["reason"] == "filesystem policy denied command"


def make_final_run(
    repository: Path,
    task_id: str,
    *,
    content: str = "safe change\n",
) -> tuple[StateStore, Path, str]:
    run_task = task(task_id)
    info = WorkspaceManager(repository).create(run_task)
    store = StateStore(repository)
    state = store.initialize_run(
        run_task,
        task_repo_root=info.worktree,
        workspace={
            "base_ref": info.base_ref,
            "base_commit": info.base_commit,
            "task_branch": info.task_branch,
            "worktree_relative_path": info.worktree_relative_path,
            "source_worktree_was_dirty": info.source_worktree_was_dirty,
        },
    )
    store.save_manifest(task_id, info.manifest())
    policy = ExecutionPolicy(repository, info)
    policy.prepare_runtime()
    store.save_permissions(
        task_id,
        policy.verify_effective(valid_effective_config(repository)),
    )
    (info.worktree / "modified.txt").write_text(content, encoding="utf-8")
    audit = AuditRecorder(store.run_dir(task_id), info.worktree, info.base_commit)
    changes = audit.capture_final_changes()
    sha = str(changes["final_diff"]["raw_sha256"])
    state.permission_verified = True
    state.last_diff_sha256 = sha
    state.diff_redaction_count = int(changes["final_diff"]["redaction_count"])
    state.mark_success("M modified.txt")
    store.save_state(state)
    result, report = ReportBuilder().build(
        run_task,
        state,
        permissions=store.load_permissions(task_id),
        changes=changes,
    )
    store.save_result(result)
    store.save_report(task_id, report)
    return store, info.worktree, sha


def test_review_is_bound_to_unchanged_diff_and_cannot_be_overwritten(
    repository: Path,
) -> None:
    store, _worktree, sha = make_final_run(repository, "review-ok")
    service = ReviewService(repository, store=store)

    review = service.record(
        "review-ok",
        decision="approved",
        reviewer="Local Reviewer",
        comment="Checked tests and diff.",
        reviewed_diff_sha256=sha,
    )

    assert review.decision is ReviewStatus.APPROVED
    assert store.load_state("review-ok").review_status is ReviewStatus.APPROVED
    assert store.load_result("review-ok").review_status is ReviewStatus.APPROVED
    assert "Local Reviewer" in (store.run_dir("review-ok") / "report.md").read_text()
    assert '"type": "review.recorded"' in (
        store.run_dir("review-ok") / "events.jsonl"
    ).read_text()
    with pytest.raises(ReviewError, match="already has"):
        service.record(
            "review-ok",
            decision="rejected",
            reviewer="Other",
            comment="Cannot overwrite",
            reviewed_diff_sha256=sha,
        )


def test_review_rejects_changed_or_redacted_diff(repository: Path) -> None:
    store, worktree, sha = make_final_run(repository, "review-changed")
    (worktree / "modified.txt").write_text("changed after report\n", encoding="utf-8")
    with pytest.raises(ReviewError, match="changed after"):
        ReviewService(repository, store=store).record(
            "review-changed",
            decision="approved",
            reviewer="Reviewer",
            comment="",
            reviewed_diff_sha256=sha,
        )

    redacted_store, _redacted_worktree, redacted_sha = make_final_run(
        repository,
        "review-secret",
        content="token=sk-abcdefghijklmnopqrstuvwxyz\n",
    )
    with pytest.raises(ReviewError, match="sensitive information"):
        ReviewService(repository, store=redacted_store).record(
            "review-secret",
            decision="rejected",
            reviewer="Reviewer",
            comment="Secret must be removed",
            reviewed_diff_sha256=redacted_sha,
        )
