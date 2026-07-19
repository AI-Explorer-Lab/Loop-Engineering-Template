from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from orchestrator.codex_loop.models import InfrastructureError, TaskSpec
from orchestrator.codex_loop.workspace import WorkspaceManager


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
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
    (tmp_path / ".gitignore").write_text(
        ".codex-orchestrator/\n.codex-runtime/\n", encoding="utf-8"
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
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


def task(task_id: str = "workspace-test") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        requirement="Change tracked file",
        acceptance_criteria=["Main remains unchanged"],
    )


def test_task_gets_pinned_branch_and_worktree_without_copying_source_wip(
    repository: Path,
) -> None:
    baseline = git(repository, "rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("human wip\n", encoding="utf-8")
    (repository / "untracked-wip.txt").write_text("human only\n", encoding="utf-8")

    workspace = WorkspaceManager(repository).create(task())

    assert workspace.base_commit == baseline
    assert workspace.task_branch == "codex/workspace-test"
    assert workspace.source_worktree_was_dirty is True
    assert git(repository, "rev-parse", "main") == baseline
    assert git(workspace.worktree, "rev-parse", "HEAD") == baseline
    assert git(workspace.worktree, "rev-parse", "--abbrev-ref", "HEAD") == workspace.task_branch
    assert (workspace.worktree / "tracked.txt").read_text() == "baseline\n"
    assert not (workspace.worktree / "untracked-wip.txt").exists()


def test_duplicate_task_and_wrong_worktree_identity_are_rejected(
    repository: Path,
) -> None:
    manager = WorkspaceManager(repository)
    workspace = manager.create(task())

    with pytest.raises(InfrastructureError, match="branch already exists"):
        manager.create(task())

    git(workspace.worktree, "checkout", "--detach", "-q")
    with pytest.raises(InfrastructureError, match="branch changed"):
        manager.verify(workspace)
