"""Create and verify one isolated Git branch/worktree per task."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import metadata
import os
from pathlib import Path
import platform
import subprocess
from typing import Any, Mapping

from .codex_client import PINNED_CODEX_RUNTIME_VERSION
from .models import InfrastructureError, SCHEMA_VERSION, TaskSpec, utc_now_iso
from .state import redact_sensitive_text


PROTECTED_BRANCHES = ("main", "master")


@dataclass(frozen=True, slots=True)
class WorkspaceInfo:
    task_id: str
    base_ref: str
    base_commit: str
    task_branch: str
    worktree: Path
    worktree_relative_path: str
    source_worktree_was_dirty: bool
    created_at: str

    def manifest(self) -> dict[str, Any]:
        try:
            sdk_version = metadata.version("openai-codex")
        except metadata.PackageNotFoundError:
            sdk_version = "unavailable"
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "repository": {
                "base_ref": self.base_ref,
                "base_commit": self.base_commit,
                "task_branch": self.task_branch,
                "worktree_relative_path": self.worktree_relative_path,
                "protected_branches": list(PROTECTED_BRANCHES),
                "source_worktree_was_dirty": self.source_worktree_was_dirty,
            },
            "runtime": {
                "codex_sdk_version": sdk_version,
                "codex_cli_version": PINNED_CODEX_RUNTIME_VERSION,
                "app_server_protocol": "v2",
                "model": "runtime-default",
                "python_version": platform.python_version(),
                "platform": platform.platform(),
            },
            "created_at": self.created_at,
        }

    @classmethod
    def from_manifest(
        cls, control_repo_root: Path, data: Mapping[str, Any]
    ) -> "WorkspaceInfo":
        repository = data.get("repository")
        if not isinstance(repository, Mapping):
            raise InfrastructureError("manifest.json has no repository object")
        relative = str(repository.get("worktree_relative_path", ""))
        worktree = (control_repo_root / relative).resolve()
        return cls(
            task_id=str(data.get("task_id", "")),
            base_ref=str(repository.get("base_ref", "HEAD")),
            base_commit=str(repository.get("base_commit", "")),
            task_branch=str(repository.get("task_branch", "")),
            worktree=worktree,
            worktree_relative_path=relative,
            source_worktree_was_dirty=bool(
                repository.get("source_worktree_was_dirty", False)
            ),
            created_at=str(data.get("created_at") or utc_now_iso()),
        )


class WorkspaceManager:
    """Own the control-repository operations that Codex must never perform."""

    def __init__(self, control_repo_root: str | Path, *, base_ref: str = "HEAD") -> None:
        self.control_repo_root = Path(control_repo_root).expanduser().resolve()
        self.base_ref = str(base_ref or "HEAD")
        self.worktrees_root = self.control_repo_root / ".codex-orchestrator/worktrees"

    def create(self, task: TaskSpec) -> WorkspaceInfo:
        self._verify_control_repository()
        base_commit = self._git("rev-parse", "--verify", f"{self.base_ref}^{{commit}}")
        if len(base_commit) != 40:
            raise InfrastructureError("Git did not return a full baseline commit SHA")

        source_dirty = bool(
            self._git("status", "--short", "--untracked-files=all").strip()
        )
        task_branch = f"codex/{task.task_id}"
        if task_branch in PROTECTED_BRANCHES:
            raise InfrastructureError("Task branch resolves to a protected branch")
        if self._branch_exists(task_branch):
            raise InfrastructureError(f"Task branch already exists: {task_branch}")

        worktree = self.worktrees_root / task.task_id
        if worktree.exists():
            raise InfrastructureError(f"Task worktree already exists: {worktree}")
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", task_branch, str(worktree), base_commit)

        info = WorkspaceInfo(
            task_id=task.task_id,
            base_ref=self.base_ref,
            base_commit=base_commit,
            task_branch=task_branch,
            worktree=worktree.resolve(),
            worktree_relative_path=worktree.relative_to(
                self.control_repo_root
            ).as_posix(),
            source_worktree_was_dirty=source_dirty,
            created_at=utc_now_iso(),
        )
        self.verify(info, require_clean=True)
        return info

    def verify(self, info: WorkspaceInfo, *, require_clean: bool = False) -> None:
        if not info.worktree.is_dir():
            raise InfrastructureError("Saved task worktree does not exist")
        actual_root = Path(
            self._git_at(info.worktree, "rev-parse", "--show-toplevel")
        ).resolve()
        if actual_root != info.worktree:
            raise InfrastructureError("Saved path is not the expected Git worktree")
        actual_branch = self._git_at(
            info.worktree, "rev-parse", "--abbrev-ref", "HEAD"
        )
        if actual_branch != info.task_branch:
            raise InfrastructureError(
                f"Task worktree branch changed ({actual_branch} != {info.task_branch})"
            )
        actual_commit = self._git_at(info.worktree, "rev-parse", "HEAD")
        if actual_commit != info.base_commit:
            # A task never commits, so HEAD must remain pinned to the baseline.
            raise InfrastructureError("Task worktree HEAD no longer matches its baseline")
        common_dir = Path(
            self._git_at(info.worktree, "rev-parse", "--git-common-dir")
        )
        if not common_dir.is_absolute():
            common_dir = (info.worktree / common_dir).resolve()
        expected_common = (self.control_repo_root / ".git").resolve()
        if common_dir.resolve() != expected_common:
            raise InfrastructureError("Task worktree points at unexpected Git metadata")
        if require_clean:
            status = self._git_at(
                info.worktree, "status", "--short", "--untracked-files=all"
            )
            if status.strip():
                raise InfrastructureError("New task worktree is not clean")

    def apply_inherited_diff(
        self,
        info: WorkspaceInfo,
        diff_path: str | Path,
        expected_sha256: str,
    ) -> None:
        """Apply an approved cumulative diff as the read-only index baseline."""

        path = Path(diff_path).expanduser().resolve()
        if not path.is_file():
            raise InfrastructureError("Approved cumulative diff does not exist")
        content = path.read_bytes()
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != str(expected_sha256):
            raise InfrastructureError("Approved cumulative diff SHA-256 changed")
        if not content:
            return
        self._run_git(
            info.worktree,
            "apply",
            "--check",
            "--index",
            "--binary",
            str(path),
        )
        self._run_git(
            info.worktree,
            "apply",
            "--index",
            "--binary",
            str(path),
        )
        staged = self._run_git(
            info.worktree,
            "diff",
            "--cached",
            "--quiet",
            allowed_exit_codes={0, 1},
        )
        if staged.returncode == 0:
            raise InfrastructureError("Approved cumulative diff produced no baseline")

    def _verify_control_repository(self) -> None:
        actual = Path(
            self._git("rev-parse", "--show-toplevel")
        ).resolve()
        if actual != self.control_repo_root:
            raise InfrastructureError("Configured control directory is not the Git root")

    def _branch_exists(self, branch: str) -> bool:
        completed = self._run_git(
            self.control_repo_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            allowed_exit_codes={0, 1},
        )
        return completed.returncode == 0

    def _git(self, *arguments: str) -> str:
        return self._run_git(self.control_repo_root, *arguments).stdout.strip()

    def _git_at(self, root: Path, *arguments: str) -> str:
        return self._run_git(root, *arguments).stdout.strip()

    @staticmethod
    def _run_git(
        root: Path,
        *arguments: str,
        allowed_exit_codes: set[int] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        allowed = allowed_exit_codes or {0}
        environment = dict(os.environ)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                shell=False,
                env=environment,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise InfrastructureError(
                f"Unable to run Git workspace operation ({type(exc).__name__})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError("Git workspace operation timed out") from exc
        if completed.returncode not in allowed:
            detail = redact_sensitive_text(
                (completed.stderr or completed.stdout).strip()
            )[:2_000]
            raise InfrastructureError(
                "Git workspace operation failed "
                f"(exit_code={completed.returncode}): {detail}"
            )
        return completed


__all__ = ["PROTECTED_BRANCHES", "WorkspaceInfo", "WorkspaceManager"]
