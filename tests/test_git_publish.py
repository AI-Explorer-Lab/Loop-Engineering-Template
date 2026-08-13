from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from codex_loop.git_publish import GitPublishService, PublishError
from codex_loop.models import DeliveryStatus, ReviewStatus, TaskSpec
from codex_loop.state import StateStore


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def prepared_task(tmp_path: Path) -> tuple[StateStore, TaskSpec, str, Path]:
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    git(repository, "commit", "-qm", "baseline")
    git(repository, "switch", "-qc", "codex/publish-test")
    (repository / "tracked.txt").write_text("published\n", encoding="utf-8")
    git(repository, "commit", "-am", "approved change", "-q")
    commit_sha = git(repository, "rev-parse", "HEAD")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(repository, "remote", "add", "origin", str(remote))

    store = StateStore(tmp_path / "control")
    task = TaskSpec(
        task_id="publish-test",
        requirement="Publish approved branch",
        acceptance_criteria=["Push exactly the committed task branch"],
    )
    state = store.initialize_run(
        task,
        task_repo_root=repository,
        workspace={"task_branch": "codex/publish-test"},
    )
    state.mark_success()
    state.review_status = ReviewStatus.APPROVED
    state.delivery_status = DeliveryStatus.ARCHIVED
    store.save_state(state)
    delivery_dir = store.run_dir(task.task_id) / "delivery"
    delivery_dir.mkdir(parents=True)
    (delivery_dir / "commit.json").write_text(
        json.dumps({"status": "committed", "commit_sha": commit_sha}),
        encoding="utf-8",
    )
    return store, task, commit_sha, remote


def test_publish_pushes_only_the_recorded_archived_commit(tmp_path: Path) -> None:
    store, task, commit_sha, remote = prepared_task(tmp_path)
    service = GitPublishService(
        tmp_path / "control", remote_name="origin", remote_url=str(remote), store=store
    )

    result = service.publish(task.task_id, commit_sha=commit_sha, reviewer="Reviewer")

    assert result["status"] == "published"
    assert result["commit_sha"] == commit_sha
    assert git(remote, "rev-parse", "refs/heads/codex/publish-test") == commit_sha
    assert service.publish(task.task_id, commit_sha=commit_sha, reviewer="Reviewer") == result


def test_publish_rejects_a_commit_other_than_delivery_evidence(tmp_path: Path) -> None:
    store, task, _commit_sha, remote = prepared_task(tmp_path)
    service = GitPublishService(
        tmp_path / "control", remote_name="origin", remote_url=str(remote), store=store
    )

    with pytest.raises(PublishError, match="confirmed commit"):
        service.publish(task.task_id, commit_sha="0" * 40, reviewer="Reviewer")


def test_auto_create_remote_binds_a_new_github_remote_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, task, _commit_sha, remote = prepared_task(tmp_path)
    repository = tmp_path / "repository"
    git(repository, "remote", "remove", "origin")
    service = GitPublishService(
        tmp_path / "control",
        remote_name="origin",
        remote_url="",
        auto_create_remote=True,
        repository_name="reading-notes",
        store=store,
    )
    monkeypatch.setattr(service, "_create_github_repository", lambda: str(remote))

    resolved = service._resolve_remote(repository)

    assert resolved == str(remote)
    assert git(repository, "remote", "get-url", "origin") == str(remote)


def test_create_github_repository_uses_authenticated_account_and_private_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _task, _commit_sha, _remote = prepared_task(tmp_path)
    service = GitPublishService(
        tmp_path / "control",
        remote_name="origin",
        remote_url="",
        auto_create_remote=True,
        repository_name="reading-notes",
        store=store,
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(program: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        assert program == "gh"
        calls.append(arguments)
        if arguments[:2] == ("api", "user"):
            return subprocess.CompletedProcess([program, *arguments], 0, "AI-Explorer-Lab\n", "")
        if arguments[:2] == ("repo", "view"):
            return subprocess.CompletedProcess([program, *arguments], 1, "", "not found")
        return subprocess.CompletedProcess([program, *arguments], 0, "created\n", "")

    monkeypatch.setattr(GitPublishService, "_run_external", staticmethod(fake_run))

    assert service._create_github_repository() == (
        "https://github.com/AI-Explorer-Lab/reading-notes.git"
    )
    assert calls[-1] == ("repo", "create", "AI-Explorer-Lab/reading-notes", "--private")


def test_new_project_publication_fast_forwards_main_and_pushes_main(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.com")
    (repository / ".gitignore").write_text(".codex-orchestrator/\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "baseline")
    baseline = git(repository, "rev-parse", "HEAD")
    task_worktree = tmp_path / "task-worktree"
    git(repository, "worktree", "add", "-b", "codex/publish-main", str(task_worktree))
    (task_worktree / "tracked.txt").write_text("published\n", encoding="utf-8")
    git(task_worktree, "commit", "-am", "approved change", "-q")
    commit_sha = git(task_worktree, "rev-parse", "HEAD")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(task_worktree, "remote", "add", "origin", str(remote))

    store = StateStore(repository)
    task = TaskSpec(
        task_id="publish-main",
        requirement="Publish new project",
        acceptance_criteria=["Publish the first result to main"],
    )
    state = store.initialize_run(
        task,
        task_repo_root=task_worktree,
        workspace={
            "base_ref": "main",
            "base_commit": baseline,
            "task_branch": "codex/publish-main",
        },
    )
    state.mark_success()
    state.review_status = ReviewStatus.APPROVED
    state.delivery_status = DeliveryStatus.ARCHIVED
    store.save_state(state)
    delivery_dir = store.run_dir(task.task_id) / "delivery"
    delivery_dir.mkdir(parents=True)
    (delivery_dir / "commit.json").write_text(
        json.dumps({"status": "committed", "commit_sha": commit_sha}),
        encoding="utf-8",
    )

    service = GitPublishService(
        repository,
        remote_name="origin",
        remote_url=str(remote),
        publish_branch="main",
        store=store,
    )
    result = service.publish(task.task_id, commit_sha=commit_sha, reviewer="Reviewer")

    assert result["branch"] == "main"
    assert result["source_branch"] == "codex/publish-main"
    assert git(repository, "rev-parse", "main") == commit_sha
    assert git(remote, "rev-parse", "refs/heads/main") == commit_sha
