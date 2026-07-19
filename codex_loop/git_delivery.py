"""Controller-owned, review-bound, idempotent task-branch commit delivery."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .audit import AuditRecorder
from .models import (
    DeliveryStatus,
    InfrastructureError,
    ReviewRecord,
    ReviewStatus,
    utc_now_iso,
)
from .state import StateStore, _atomic_write_json, redact_sensitive_text
from .workspace import PROTECTED_BRANCHES


class DeliveryError(InfrastructureError):
    """A commit gate failure that must never trigger automatic Git rewriting."""


class GitDeliveryService:
    """Create exactly one cumulative snapshot commit after human approval."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        store: StateStore | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.store = store or StateStore(self.repo_root)

    def deliver(
        self,
        task_id: str,
        *,
        review: ReviewRecord | None = None,
    ) -> dict[str, Any]:
        lock = self.store.acquire_active_lock(task_id)
        try:
            state = self.store.load_state(task_id)
            task = self.store.load_task(task_id)
            selected_review = review or (
                self.store.load_latest_review(task_id)
                if task.queue_id is not None
                else self.store.load_review(task_id)
            )
            if selected_review.decision is not ReviewStatus.APPROVED:
                raise DeliveryError("only an approved review can create a commit")
            if not selected_review.commit_subject:
                raise DeliveryError("approved review has no commit subject")
            if state.review_status is not ReviewStatus.APPROVED:
                raise DeliveryError("run state is not approved")
            run_dir = self.store.run_dir(task_id)
            record_path = run_dir / "delivery" / "commit.json"
            existing = self._optional_json(record_path)
            worktree = Path(state.repo_root).resolve()
            if existing and existing.get("status") == "committed":
                self._verify_completed(existing, state, worktree)
                state.delivery_status = DeliveryStatus.COMMITTED
                self.store.save_state(state)
                return existing

            audit = AuditRecorder(
                run_dir,
                worktree,
                state.base_commit,
                inherited_baseline=state.inherited_baseline,
                queue_task=task.queue_id is not None,
            )
            changes = self._read_json(run_dir / "changes" / "files.json")
            expected_diff_path, expected_cumulative_sha = self._expected_diff(
                run_dir, changes, task.queue_id is not None
            )
            expected_bytes = expected_diff_path.read_bytes()
            reviewed_files = self._reviewed_files(changes)
            review_sha = _json_sha(selected_review.to_dict())
            branch = self._git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
            if branch != state.task_branch or branch in PROTECTED_BRANCHES:
                raise DeliveryError("task worktree is not on its recorded task branch")
            head = self._git(worktree, "rev-parse", "HEAD")

            if existing:
                return self._recover_or_continue(
                    existing=existing,
                    record_path=record_path,
                    state=state,
                    worktree=worktree,
                    audit=audit,
                    expected_bytes=expected_bytes,
                    expected_cumulative_sha=expected_cumulative_sha,
                    review=selected_review,
                    review_sha=review_sha,
                    reviewed_files=reviewed_files,
                    current_head=head,
                )
            if head != state.base_commit:
                raise DeliveryError("task branch HEAD changed before commit delivery")
            self._verify_live_diff(
                audit,
                selected_review,
                changes,
                reviewed_files,
                expected_cumulative_sha,
                expected_bytes,
            )
            staged_before = self._git_bytes(
                worktree,
                "diff",
                "--cached",
                "--binary",
                "--find-renames",
                state.base_commit,
                "--",
            )
            staged_before_matches = bool(staged_before) and _git_diffs_equivalent(
                staged_before, expected_bytes
            )
            if not staged_before_matches:
                self._verify_index_baseline(worktree, state)
            state.delivery_status = DeliveryStatus.COMMITTING
            self.store.save_state(state)
            if not audit.has_event("commit.started"):
                audit.append(
                    "commit.started",
                    {
                        "pre_commit_head": head,
                        "reviewed_diff_sha256": selected_review.reviewed_diff_sha256,
                        "commit_subject": selected_review.commit_subject,
                    },
                    source="orchestrator",
                )
            if not staged_before_matches:
                self._stage_reviewed_files(worktree, reviewed_files)
            staged = self._git_bytes(
                worktree,
                "diff",
                "--cached",
                "--binary",
                "--find-renames",
                state.base_commit,
                "--",
            )
            if not staged:
                raise DeliveryError("approved change produced an empty staged diff")
            if not _git_diffs_equivalent(staged, expected_bytes):
                raise DeliveryError(
                    "staged cumulative diff does not match the approved content"
                )
            staged_sha = hashlib.sha256(staged).hexdigest()
            tree = self._git(worktree, "write-tree")
            author = self._identity(worktree)
            intent = {
                "schema_version": 1,
                "status": "committing",
                "task_id": task_id,
                "queue_id": task.queue_id,
                "branch": branch,
                "pre_commit_head": head,
                "expected_tree": tree,
                "subject": selected_review.commit_subject,
                "review_sha256": review_sha,
                "reviewed_diff_sha256": selected_review.reviewed_diff_sha256,
                "cumulative_diff_sha256": expected_cumulative_sha,
                "staged_diff_sha256": staged_sha,
                "reviewed_files": reviewed_files,
                "author": author,
                "started_at": utc_now_iso(),
            }
            _atomic_write_json(record_path, intent)
            return self._commit_and_record(
                intent, record_path, state, worktree, audit
            )
        except Exception as exc:
            self._mark_failed(task_id, exc)
            raise
        finally:
            self.store.release_active_lock(lock)

    def _recover_or_continue(
        self,
        *,
        existing: dict[str, Any],
        record_path: Path,
        state: Any,
        worktree: Path,
        audit: AuditRecorder,
        expected_bytes: bytes,
        expected_cumulative_sha: str,
        review: ReviewRecord,
        review_sha: str,
        reviewed_files: list[str],
        current_head: str,
    ) -> dict[str, Any]:
        for key, expected in (
            ("task_id", state.task_id),
            ("branch", state.task_branch),
            ("pre_commit_head", state.base_commit),
            ("subject", review.commit_subject),
            ("review_sha256", review_sha),
            ("reviewed_diff_sha256", review.reviewed_diff_sha256),
            ("cumulative_diff_sha256", expected_cumulative_sha),
        ):
            if str(existing.get(key, "")) != str(expected):
                raise DeliveryError(f"commit recovery intent changed: {key}")
        if list(existing.get("reviewed_files", [])) != reviewed_files:
            raise DeliveryError("commit recovery reviewed file set changed")
        if current_head != state.base_commit:
            return self._recover_created_commit(
                existing,
                record_path,
                state,
                worktree,
                audit,
                current_head,
                recovered=True,
            )
        staged = self._git_bytes(
            worktree,
            "diff",
            "--cached",
            "--binary",
            "--find-renames",
            state.base_commit,
            "--",
        )
        if not _git_diffs_equivalent(staged, expected_bytes):
            raise DeliveryError("staged tree changed during commit recovery")
        if hashlib.sha256(staged).hexdigest() != str(
            existing.get("staged_diff_sha256", "")
        ):
            raise DeliveryError("staged diff changed during commit recovery")
        tree = self._git(worktree, "write-tree")
        if tree != str(existing.get("expected_tree", "")):
            raise DeliveryError("staged tree does not match commit recovery intent")
        state.delivery_status = DeliveryStatus.COMMITTING
        self.store.save_state(state)
        return self._commit_and_record(existing, record_path, state, worktree, audit)

    def _commit_and_record(
        self,
        intent: dict[str, Any],
        record_path: Path,
        state: Any,
        worktree: Path,
        audit: AuditRecorder,
    ) -> dict[str, Any]:
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_EDITOR": "/usr/bin/true",
                "GIT_SEQUENCE_EDITOR": "/usr/bin/true",
            }
        )
        self._run_git(
            worktree,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "-m",
            str(intent["subject"]),
            environment=environment,
        )
        commit_sha = self._git(worktree, "rev-parse", "HEAD")
        return self._recover_created_commit(
            intent,
            record_path,
            state,
            worktree,
            audit,
            commit_sha,
            recovered=False,
        )

    def _recover_created_commit(
        self,
        intent: dict[str, Any],
        record_path: Path,
        state: Any,
        worktree: Path,
        audit: AuditRecorder,
        commit_sha: str,
        *,
        recovered: bool,
    ) -> dict[str, Any]:
        parent = self._git(worktree, "rev-parse", f"{commit_sha}^")
        tree = self._git(worktree, "rev-parse", f"{commit_sha}^{{tree}}")
        subject = self._git(worktree, "show", "-s", "--format=%s", commit_sha)
        branch = self._git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
        if parent != str(intent["pre_commit_head"]):
            raise DeliveryError("unknown HEAD parent found during commit recovery")
        if tree != str(intent["expected_tree"]):
            raise DeliveryError("unknown HEAD tree found during commit recovery")
        if subject != str(intent["subject"]):
            raise DeliveryError("unknown HEAD subject found during commit recovery")
        if branch != str(intent["branch"]):
            raise DeliveryError("task branch changed during commit recovery")
        if self._git(worktree, "status", "--porcelain", "--untracked-files=all"):
            raise DeliveryError("task worktree is not clean after commit")
        metadata = self._commit_metadata(worktree, commit_sha)
        record = {
            **intent,
            "status": "committed",
            "commit_sha": commit_sha,
            "parent": parent,
            "tree": tree,
            "author": {
                "name": metadata["author_name"],
                "email": metadata["author_email"],
            },
            "committer": {
                "name": metadata["committer_name"],
                "email": metadata["committer_email"],
            },
            "committed_at": metadata["committed_at"],
            "recovered": recovered,
        }
        record.pop("error", None)
        _atomic_write_json(record_path, record)
        state.delivery_status = DeliveryStatus.COMMITTED
        state.infrastructure_error = None
        self.store.save_state(state)
        if not audit.has_event("commit.completed"):
            audit.append(
                "commit.completed",
                {
                    "commit_sha": commit_sha,
                    "parent": parent,
                    "tree": tree,
                    "review_sha256": intent["review_sha256"],
                },
            )
        return record

    def _verify_completed(
        self, record: Mapping[str, Any], state: Any, worktree: Path
    ) -> None:
        if self._git(worktree, "rev-parse", "--abbrev-ref", "HEAD") != state.task_branch:
            raise DeliveryError("committed task branch changed")
        if self._git(worktree, "rev-parse", "HEAD") != str(record.get("commit_sha")):
            raise DeliveryError("committed task HEAD changed")
        if self._git(worktree, "rev-parse", "HEAD^") != state.base_commit:
            raise DeliveryError("committed task parent changed")
        if self._git(worktree, "rev-parse", "HEAD^{tree}") != str(record.get("tree")):
            raise DeliveryError("committed task tree changed")

    def _verify_live_diff(
        self,
        audit: AuditRecorder,
        review: ReviewRecord,
        changes: Mapping[str, Any],
        reviewed_files: list[str],
        expected_cumulative_sha: str,
        expected_bytes: bytes,
    ) -> None:
        final = dict(changes.get("final_diff", {}))
        saved = str(final.get("raw_sha256", ""))
        if saved != review.reviewed_diff_sha256:
            raise DeliveryError("review is not bound to the saved final diff")
        saved_path = audit.run_dir / str(final.get("path", ""))
        if not saved_path.is_file():
            raise DeliveryError("approved final diff file is missing")
        saved_bytes = saved_path.read_bytes()
        if hashlib.sha256(saved_bytes).hexdigest() != saved:
            raise DeliveryError("approved final diff file hash changed")
        if not _git_diffs_equivalent(audit.current_diff_bytes(), saved_bytes):
            raise DeliveryError("task diff changed after human review")
        if audit.changed_paths() != reviewed_files:
            raise DeliveryError("task contains files outside the reviewed file set")
        if audit.queue_task:
            if hashlib.sha256(expected_bytes).hexdigest() != expected_cumulative_sha:
                raise DeliveryError("queue cumulative diff file hash changed")
            if not _git_diffs_equivalent(
                audit.current_cumulative_diff_bytes(), expected_bytes
            ):
                raise DeliveryError("queue cumulative diff changed after review")

    def _verify_index_baseline(self, worktree: Path, state: Any) -> None:
        staged = self._git_bytes(
            worktree,
            "diff",
            "--cached",
            "--binary",
            "--find-renames",
            state.base_commit,
            "--",
        )
        if state.inherited_baseline:
            if hashlib.sha256(staged).hexdigest() != state.inherited_diff_sha256:
                raise DeliveryError("queue inherited staged baseline changed")
        elif staged:
            raise DeliveryError("single task has unexpected pre-staged content")

    @staticmethod
    def _stage_reviewed_files(worktree: Path, reviewed_files: list[str]) -> None:
        if not reviewed_files:
            raise DeliveryError("reviewed file set is empty")
        GitDeliveryService._run_git(worktree, "add", "--all", "--", *reviewed_files)

    @staticmethod
    def _reviewed_files(changes: Mapping[str, Any]) -> list[str]:
        values: list[str] = []
        for item in changes.get("files", []):
            if not isinstance(item, Mapping):
                raise DeliveryError("changes file metadata is invalid")
            path = str(item.get("path", ""))
            raw = Path(path)
            if (
                not path
                or raw.is_absolute()
                or ".." in raw.parts
                or raw.parts[0] in {".git", ".codex-orchestrator", ".codex-runtime"}
            ):
                raise DeliveryError("reviewed file path is unsafe")
            values.append(raw.as_posix())
        if len(values) != len(set(values)):
            raise DeliveryError("reviewed file set contains duplicates")
        return sorted(values)

    @staticmethod
    def _expected_diff(
        run_dir: Path, changes: Mapping[str, Any], queue_task: bool
    ) -> tuple[Path, str]:
        key = "cumulative_diff" if queue_task else "final_diff"
        metadata = changes.get(key)
        if not isinstance(metadata, Mapping):
            raise DeliveryError(f"changes metadata has no {key}")
        if int(metadata.get("redaction_count", 0)):
            raise DeliveryError("redacted content cannot be committed automatically")
        expected_relative = (
            "changes/cumulative.diff" if queue_task else "changes/final.diff"
        )
        if str(metadata.get("path", "")) != expected_relative:
            raise DeliveryError("approved diff path is invalid")
        path = run_dir / expected_relative
        if not path.is_file():
            raise DeliveryError("approved diff file is missing")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(metadata.get("raw_sha256", "")):
            raise DeliveryError("approved diff file hash changed")
        return path, digest

    @staticmethod
    def _identity(worktree: Path) -> dict[str, str]:
        name_result = GitDeliveryService._run_git(
            worktree, "config", "--get", "user.name", allowed_exit_codes={0, 1}
        )
        email_result = GitDeliveryService._run_git(
            worktree, "config", "--get", "user.email", allowed_exit_codes={0, 1}
        )
        name = name_result.stdout.decode("utf-8", errors="replace").strip()
        email = email_result.stdout.decode("utf-8", errors="replace").strip()
        if not name or not email:
            raise DeliveryError("repository Git identity is missing")
        return {"name": name, "email": email}

    @staticmethod
    def _commit_metadata(worktree: Path, commit_sha: str) -> dict[str, str]:
        raw = GitDeliveryService._git(
            worktree,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%cn%x00%ce%x00%cI",
            commit_sha,
        )
        parts = raw.split("\x00")
        if len(parts) != 5:
            raise DeliveryError("Git returned incomplete commit metadata")
        return {
            "author_name": parts[0],
            "author_email": parts[1],
            "committer_name": parts[2],
            "committer_email": parts[3],
            "committed_at": parts[4],
        }

    def _mark_failed(self, task_id: str, error: Exception) -> None:
        try:
            state = self.store.load_state(task_id)
        except Exception:
            return
        state.delivery_status = DeliveryStatus.FAILED
        message = redact_sensitive_text(str(error) or type(error).__name__)[:2_000]
        integrity_markers = (
            "unknown HEAD",
            "branch HEAD changed",
            "branch changed",
            "tree changed",
            "tree does not match",
            "parent changed",
            "committed task HEAD changed",
        )
        if isinstance(error, DeliveryError) and any(
            marker in message for marker in integrity_markers
        ):
            state.mark_infrastructure_error(message)
        else:
            state.last_error_summary = message
        self.store.save_state(state)
        path = self.store.run_dir(task_id) / "delivery" / "commit.json"
        existing = self._optional_json(path)
        if existing:
            _atomic_write_json(path, {**existing, "status": "failed", "error": message})

    @staticmethod
    def _optional_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        return GitDeliveryService._read_json(path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryError(f"delivery artifact is unreadable: {path.name}") from exc
        if not isinstance(value, dict):
            raise DeliveryError(f"delivery artifact must be an object: {path.name}")
        return value

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        return GitDeliveryService._run_git(root, *arguments).stdout.decode(
            "utf-8", errors="replace"
        ).strip()

    @staticmethod
    def _git_bytes(root: Path, *arguments: str) -> bytes:
        return GitDeliveryService._run_git(root, *arguments).stdout

    @staticmethod
    def _run_git(
        root: Path,
        *arguments: str,
        environment: Mapping[str, str] | None = None,
        allowed_exit_codes: set[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                timeout=60,
                check=False,
                shell=False,
                env=None if environment is None else dict(environment),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise DeliveryError("unable to run Git delivery operation") from exc
        except subprocess.TimeoutExpired as exc:
            raise DeliveryError("Git delivery operation timed out") from exc
        allowed = allowed_exit_codes or {0}
        if completed.returncode not in allowed:
            detail = redact_sensitive_text(
                completed.stderr.decode("utf-8", errors="replace")
                or completed.stdout.decode("utf-8", errors="replace")
            )[:2_000]
            raise DeliveryError(
                f"Git delivery operation failed (exit_code={completed.returncode}): {detail}"
            )
        return completed


def _json_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _git_diffs_equivalent(left: bytes, right: bytes) -> bool:
    """Compare exact per-file Git diff blocks while ignoring block order."""

    try:
        return _git_diff_blocks(left) == _git_diff_blocks(right)
    except ValueError:
        return False


def _git_diff_blocks(raw: bytes) -> dict[bytes, bytes]:
    if not raw:
        return {}
    prefix = b"diff --git "
    blocks: dict[bytes, bytes] = {}
    current: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        if line.startswith(prefix):
            if current:
                _store_git_diff_block(blocks, current)
            current = [line]
            continue
        if not current:
            raise ValueError("Git diff has content before its first file header")
        current.append(line)
    if current:
        _store_git_diff_block(blocks, current)
    return blocks


def _store_git_diff_block(
    blocks: dict[bytes, bytes], lines: list[bytes]
) -> None:
    header = lines[0].rstrip(b"\r\n")
    if header in blocks:
        raise ValueError("Git diff contains duplicate file headers")
    blocks[header] = b"".join(lines)


__all__ = ["DeliveryError", "GitDeliveryService"]
