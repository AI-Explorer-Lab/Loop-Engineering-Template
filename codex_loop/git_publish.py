"""Explicit, review-bound publication of an already archived task branch."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

from .models import DeliveryStatus, InfrastructureError, ReviewStatus, utc_now_iso
from .state import StateStore, _atomic_write_json
from .workspace import HARNESS_RUNTIME_PATHSPEC


class PublishError(InfrastructureError):
    """A publication gate failure; it must not rewrite a task branch."""


class GitPublishService:
    """Push one immutable task-branch commit to one configured remote."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        remote_name: str,
        remote_url: str,
        store: StateStore | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.remote_name = remote_name.strip()
        self.remote_url = remote_url.strip()
        self.store = store or StateStore(self.repo_root)

    def publish(self, task_id: str, *, commit_sha: str, reviewer: str) -> dict[str, Any]:
        if not self.remote_name or not self.remote_url:
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
                if existing.get("commit_sha") != expected_sha or existing.get("remote_url") != self.remote_url:
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
            if self._git(worktree, "remote", "get-url", self.remote_name) != self.remote_url:
                raise PublishError("configured publication remote does not match repository origin")
            self._git(worktree, "push", self.remote_name, state.task_branch)
            result = {
                "schema_version": 1,
                "status": "published",
                "task_id": task_id,
                "branch": state.task_branch,
                "commit_sha": expected_sha,
                "remote_name": self.remote_name,
                "remote_url": self.remote_url,
                "reviewer": reviewer,
                "published_at": utc_now_iso(),
            }
            _atomic_write_json(publish_path, result)
            self.store.append_event(task_id, "delivery.published", result)
            return result
        finally:
            self.store.release_active_lock(lock)

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
