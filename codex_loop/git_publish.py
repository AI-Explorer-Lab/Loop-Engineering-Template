"""Explicit, review-bound publication of an already archived task branch."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlparse

from .models import DeliveryStatus, InfrastructureError, ReviewStatus, utc_now_iso
from .state import StateStore, _atomic_write_json
from .workspace import HARNESS_RUNTIME_PATHSPEC


class PublishError(InfrastructureError):
    """A publication gate failure; it must not rewrite a task branch."""


class GitPublishService:
    """Push one immutable task-branch commit to a fixed or explicitly-created remote."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        remote_name: str,
        remote_url: str,
        store: StateStore | None = None,
        auto_create_remote: bool = False,
        repository_name: str = "",
        visibility: str = "private",
        publish_branch: str = "",
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.remote_name = remote_name.strip()
        self.remote_url = remote_url.strip()
        self.store = store or StateStore(self.repo_root)
        self.auto_create_remote = auto_create_remote
        self.repository_name = repository_name.strip() or self.repo_root.name
        self.visibility = visibility.strip().lower() or "private"
        self.publish_branch = publish_branch.strip()
        if self.visibility not in {"private", "public", "internal"}:
            raise ValueError("publication visibility must be private, public, or internal")

    def publish(self, task_id: str, *, commit_sha: str, reviewer: str) -> dict[str, Any]:
        if not self.remote_name:
            raise PublishError("publication is not configured for this project")
        if not self.remote_url and not self.auto_create_remote:
            raise PublishError("publication is not configured for this project")
        lock = self.store.acquire_active_lock(task_id)
        try:
            task = self.store.load_task(task_id)
            state = self.store.load_state(task_id)
            if task.queue_id is not None:
                raise PublishError("queue subtasks cannot be published individually")
            if state.review_status is not ReviewStatus.APPROVED:
                raise PublishError("only an approved task can be published")
            if state.delivery_status is not DeliveryStatus.ARCHIVED:
                raise PublishError("task archive is not complete")
            record_path = self.store.run_dir(task_id) / "delivery" / "commit.json"
            publish_path = self.store.run_dir(task_id) / "delivery" / "publish.json"
            commit = self._json(record_path)
            expected_sha = str(commit.get("commit_sha", ""))
            if commit.get("status") != "committed" or not expected_sha:
                raise PublishError("committed delivery evidence is missing")
            if commit_sha != expected_sha:
                raise PublishError("confirmed commit does not match delivery evidence")
            existing = self._json(publish_path)
            if existing.get("status") == "published":
                if existing.get("commit_sha") != expected_sha:
                    raise PublishError("existing publication record does not match delivery evidence")
                if self.remote_url and existing.get("remote_url") != self.remote_url:
                    raise PublishError("existing publication record does not match delivery evidence")
                return existing
            worktree = Path(state.repo_root).resolve()
            if self._git(worktree, "rev-parse", "--abbrev-ref", "HEAD") != state.task_branch:
                raise PublishError("task worktree is not on its recorded task branch")
            if self._git(worktree, "rev-parse", "HEAD") != expected_sha:
                raise PublishError("task branch HEAD does not match committed delivery evidence")
            if self._git(
                worktree,
                "status",
                "--porcelain",
                "--",
                ".",
                HARNESS_RUNTIME_PATHSPEC,
            ):
                raise PublishError("task worktree is not clean")
            publication_url = self._resolve_remote(worktree)
            target_branch = self._publication_branch(state.task_branch)
            if target_branch == state.task_branch:
                self._git(worktree, "push", self.remote_name, state.task_branch)
            else:
                self._fast_forward_source_branch(
                    state,
                    target_branch=target_branch,
                    commit_sha=expected_sha,
                )
                self._git(
                    worktree,
                    "push",
                    self.remote_name,
                    f"{state.task_branch}:{target_branch}",
                )
            result = {
                "schema_version": 1,
                "status": "published",
                "task_id": task_id,
                "branch": target_branch,
                "source_branch": state.task_branch,
                "commit_sha": expected_sha,
                "remote_name": self.remote_name,
                "remote_url": publication_url,
                "reviewer": reviewer,
                "published_at": utc_now_iso(),
            }
            _atomic_write_json(publish_path, result)
            self.store.append_event(task_id, "delivery.published", result)
            return result
        finally:
            self.store.release_active_lock(lock)

    def _publication_branch(self, task_branch: str) -> str:
        branch = self.publish_branch or task_branch
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or ".." in branch
            or "@{" in branch
            or branch.endswith(("/", "."))
            or "//" in branch
        ):
            raise PublishError(f"publication branch is invalid: {branch}")
        return branch

    def _fast_forward_source_branch(
        self,
        state: Any,
        *,
        target_branch: str,
        commit_sha: str,
    ) -> None:
        """Advance a newly-created project's checked-out main branch safely."""

        source_root = Path(state.control_repo_root or self.repo_root).resolve()
        actual_root = Path(
            self._git(source_root, "rev-parse", "--show-toplevel")
        ).resolve()
        if actual_root != source_root:
            raise PublishError("configured source repository root is invalid")
        current_branch = self._git(
            source_root, "rev-parse", "--abbrev-ref", "HEAD"
        )
        if current_branch != target_branch:
            raise PublishError(
                f"source repository must be checked out on {target_branch} before first publication"
            )
        if self._git(
            source_root,
            "status",
            "--porcelain",
            "--",
            ".",
            HARNESS_RUNTIME_PATHSPEC,
        ):
            raise PublishError("source repository is not clean")
        current_commit = self._git(source_root, "rev-parse", "HEAD")
        if current_commit == commit_sha:
            return
        if not state.base_commit or current_commit != state.base_commit:
            raise PublishError(
                f"source {target_branch} changed after the task baseline"
            )
        self._git(source_root, "merge", "--ff-only", commit_sha)
        if self._git(source_root, "rev-parse", "HEAD") != commit_sha:
            raise PublishError(f"source {target_branch} did not reach the published commit")

    def _resolve_remote(self, worktree: Path) -> str:
        """Return the exact remote, creating a GitHub repository only when needed."""

        current_remote = self._git_optional(
            worktree, "remote", "get-url", self.remote_name
        )
        if self.remote_url:
            if current_remote != self.remote_url:
                raise PublishError(
                    "configured publication remote does not match repository origin"
                )
            return self.remote_url
        if current_remote:
            if not self._is_github_remote(current_remote):
                raise PublishError(
                    "repository already has a non-GitHub remote; configure the exact GitHub remote before publishing"
                )
            self.remote_url = self._canonical_github_url(current_remote)
            return self.remote_url
        if not self.auto_create_remote:
            raise PublishError("publication is not configured for this project")

        remote_url = self._create_github_repository()
        self._git(worktree, "remote", "add", self.remote_name, remote_url)
        self.remote_url = remote_url
        return remote_url

    def _create_github_repository(self) -> str:
        """Create or adopt the authenticated user's repository without pushing yet."""

        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", self.repository_name
        ):
            raise PublishError(
                f"project directory is not a valid GitHub repository name: {self.repository_name}"
            )
        owner = self._gh("api", "user", "--jq", ".login")
        if not owner or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
            raise PublishError("GitHub account name could not be determined")
        repository = f"{owner}/{self.repository_name}"
        existing = self._gh_optional(
            "repo", "view", repository, "--json", "url", "--jq", ".url"
        )
        if existing:
            return self._canonical_github_url(existing)
        self._gh("repo", "create", repository, f"--{self.visibility}")
        return f"https://github.com/{repository}.git"

    @staticmethod
    def _is_github_remote(value: str) -> bool:
        normalized = value.strip()
        if normalized.startswith("git@github.com:"):
            return bool(normalized.removeprefix("git@github.com:").strip("/"))
        parsed = urlparse(normalized)
        return parsed.hostname == "github.com" and bool(parsed.path.strip("/"))

    @staticmethod
    def _canonical_github_url(value: str) -> str:
        normalized = value.strip().removesuffix("/")
        if normalized.startswith("git@github.com:"):
            path = normalized.removeprefix("git@github.com:").strip("/")
            return f"https://github.com/{path.removesuffix('.git')}.git"
        parsed = urlparse(normalized)
        if parsed.hostname != "github.com":
            raise PublishError("GitHub repository URL is invalid")
        path = parsed.path.strip("/").removesuffix(".git")
        return f"https://github.com/{path}.git"

    @staticmethod
    def _gh(*arguments: str) -> str:
        completed = GitPublishService._run_external("gh", *arguments)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gh command failed").strip()
            raise PublishError(f"GitHub repository setup failed: {detail[-500:]}")
        return completed.stdout.strip()

    @staticmethod
    def _gh_optional(*arguments: str) -> str:
        completed = GitPublishService._run_external("gh", *arguments)
        if completed.returncode == 0:
            return completed.stdout.strip()
        detail = (completed.stderr or completed.stdout or "").lower()
        if "not found" in detail or "could not resolve" in detail:
            return ""
        raise PublishError(
            "GitHub repository setup failed while checking the repository; run `gh auth status` and re-authenticate if needed"
        )

    @staticmethod
    def _run_external(
        program: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GH_PROMPT_DISABLED"] = "1"
        try:
            return subprocess.run(
                [program, *arguments],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                shell=False,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise PublishError(
                "GitHub CLI `gh` is unavailable; install it and run `gh auth login -h github.com`"
            ) from exc

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        import json
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _git(worktree: Path, *arguments: str) -> str:
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", *arguments], cwd=worktree, env=environment,
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "git command failed").strip()
            raise PublishError(message) from exc
        return completed.stdout.strip()

    @staticmethod
    def _git_optional(worktree: Path, *arguments: str) -> str:
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=worktree,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise PublishError("Git is unavailable") from exc
        if completed.returncode == 0:
            return completed.stdout.strip()
        if arguments[:2] == ("remote", "get-url"):
            return ""
        message = (completed.stderr or completed.stdout or "git command failed").strip()
        raise PublishError(message)
