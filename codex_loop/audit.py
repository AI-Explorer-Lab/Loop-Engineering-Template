"""Append-only events and complete, secret-safe run artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Mapping

from .models import AuditEvent, InfrastructureError, SCHEMA_VERSION
from .policy import ExecutionPolicy
from .state import (
    _atomic_write_json,
    _atomic_write_text,
    redact_sensitive_data,
    redact_sensitive_text,
)


class AuditRecorder:
    """Write one task's timeline and derived diff artifacts."""

    def __init__(
        self,
        run_dir: str | Path,
        worktree: str | Path,
        base_commit: str,
        *,
        inherited_baseline: bool = False,
        queue_task: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.worktree = Path(worktree).resolve()
        self.base_commit = str(base_commit)
        self.inherited_baseline = bool(inherited_baseline)
        self.queue_task = bool(queue_task)
        self.events_path = self.run_dir / "events.jsonl"
        self._next_seq = self._load_next_seq()

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        source: str = "orchestrator",
        turn_number: int | None = None,
        round_number: int | None = None,
        redacted: bool = False,
    ) -> AuditEvent:
        event = AuditEvent(
            seq=self._next_seq,
            source=source,
            type=event_type,
            payload=dict(payload or {}),
            turn_number=turn_number,
            round_number=round_number,
            redacted=redacted,
        )
        safe = redact_sensitive_data(event.to_dict())
        line = json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._next_seq += 1
        return event

    def save_prompt(self, turn_number: int, prompt: str) -> Path:
        path = self._turn_dir(turn_number) / "prompt.md"
        _atomic_write_text(path, redact_sensitive_text(prompt))
        self.append(
            "prompt.saved",
            {
                "path": path.relative_to(self.run_dir).as_posix(),
                "sha256": _sha256_bytes(path.read_bytes()),
            },
            turn_number=turn_number,
            redacted=True,
        )
        return path

    def save_response(self, turn_number: int, response: str | None) -> Path:
        path = self._turn_dir(turn_number) / "response.md"
        _atomic_write_text(path, redact_sensitive_text(response or ""))
        return path

    def record_codex_notification(self, turn_number: int, notification: Any) -> None:
        method = str(getattr(notification, "method", "unknown"))
        normalized_method = method.casefold()
        # The pinned protocol uses both ``.../delta`` and camel-cased
        # ``...TextDelta`` notification names. Reasoning bodies and summaries
        # are deliberately excluded from the audit trail in every form.
        if normalized_method.endswith("/delta") or normalized_method.startswith(
            "item/reasoning/"
        ):
            return
        payload = _jsonable(getattr(notification, "payload", {}))
        self._record_notification_payload(turn_number, method, payload)

    def record_permission_denial(
        self,
        turn_number: int,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        category = "filesystem" if "fileChange" in method else "command"
        self.append(
            "permission.denied",
            {
                "category": category,
                "target": dict(params),
                "reason": "App Server approval request denied by deny_all",
                "request_method": method,
            },
            source="codex",
            turn_number=turn_number,
            redacted=True,
        )

    def backfill_completed_items(
        self, turn_number: int, items: list[dict[str, Any]]
    ) -> int:
        """Append history items missing after a process interruption."""

        existing_items = self._completed_item_keys(turn_number)
        appended = 0
        for raw_item in items:
            item = _unwrap_item(raw_item)
            item_key = _item_key(item)
            if item_key in existing_items:
                continue
            self._record_notification_payload(
                turn_number,
                "item/completed",
                {"item": item, "recovered_from_history": True},
            )
            existing_items.add(item_key)
            appended += 1
        return appended

    def load_prompt(self, turn_number: int) -> str:
        path = self._turn_dir(turn_number) / "prompt.md"
        if not path.is_file():
            raise InfrastructureError(
                f"Saved prompt for turn {turn_number} is missing"
            )
        return path.read_text(encoding="utf-8")

    def turn_checkpoint_paths(self, turn_number: int) -> set[str]:
        event = self._last_event("turn.started", turn_number=turn_number)
        if event is None:
            return set()
        payload = event.get("payload", {})
        return {
            str(path)
            for path in payload.get("changed_paths", [])
            if isinstance(path, str)
        }

    def codex_changed_paths(self, turn_number: int) -> set[str]:
        paths: set[str] = set()
        for event in self._events():
            if event.get("type") != "file.changed" or event.get(
                "turn_number"
            ) != turn_number:
                continue
            item = event.get("payload", {}).get("item", {})
            for change in item.get("changes", []):
                if not isinstance(change, Mapping) or not change.get("path"):
                    continue
                relative = self._relative_worktree_path(str(change["path"]))
                if relative is not None:
                    paths.add(relative)
        return paths

    def latest_recorded_worktree_diff_sha256(
        self, turn_number: int
    ) -> str | None:
        latest: str | None = None
        for event in self._events():
            if event.get("turn_number") != turn_number:
                continue
            payload = event.get("payload", {})
            candidate = payload.get("worktree_diff_sha256")
            if isinstance(candidate, str) and candidate:
                latest = candidate
        return latest

    def _record_notification_payload(
        self, turn_number: int, method: str, payload: Any
    ) -> None:
        item = payload.get("item") if isinstance(payload, Mapping) else None
        item = _unwrap_item(item) if isinstance(item, Mapping) else item
        if isinstance(payload, Mapping) and isinstance(item, Mapping):
            payload = dict(payload)
            payload["item"] = item
        item_type = _item_type(item)
        if item_type == "reasoning":
            payload = {"reasoning_content_omitted": True}
            item = None
        base_payload = {
            "method": method,
            "item_type": item_type,
            "data": payload,
        }
        if method == "turn/diff/updated":
            base_payload["worktree_diff_sha256"] = self.current_diff_sha256()
        event_type = (
            "codex.item.unknown"
            if method != "item/completed" or item_type == "unknown"
            else "codex.item.completed"
        )
        self.append(
            event_type,
            base_payload,
            source="codex",
            turn_number=turn_number,
            redacted=True,
        )

        if method == "item/completed" and isinstance(item, Mapping):
            if item_type == "commandExecution":
                self._record_codex_command(turn_number, item)
            elif item_type == "fileChange":
                self._record_file_change(turn_number, item)

    def current_diff_sha256(self) -> str:
        return _sha256_bytes(self.current_diff_bytes())

    def current_diff_bytes(self) -> bytes:
        return self._complete_diff()

    def current_cumulative_diff_sha256(self) -> str:
        return _sha256_bytes(self.current_cumulative_diff_bytes())

    def current_cumulative_diff_bytes(self) -> bytes:
        return self._cumulative_diff()

    def changed_paths(self) -> list[str]:
        paths = set(self._name_status())
        paths.update(self._untracked_paths())
        return sorted(paths)

    def current_diff_text(self) -> str:
        """Return the current complete diff for a bounded evaluator excerpt."""

        return self._complete_diff().decode("utf-8", errors="replace")

    def current_changed_files(self) -> list[dict[str, Any]]:
        """Return current file metadata without persisting final artifacts."""

        return self._changed_files()

    def has_event(
        self,
        event_type: str,
        *,
        turn_number: int | None = None,
        round_number: int | None = None,
    ) -> bool:
        return self._last_event(
            event_type,
            turn_number=turn_number,
            round_number=round_number,
        ) is not None

    def turn_prompt_kind(self, turn_number: int) -> str | None:
        """Return the prompt kind recorded for a started Codex turn."""

        event = self._last_event("turn.started", turn_number=turn_number)
        if event is None:
            return None
        payload = event.get("payload", {})
        value = payload.get("prompt_kind") if isinstance(payload, Mapping) else None
        return str(value) if value else None

    def capture_final_changes(self) -> dict[str, Any]:
        raw_diff = self._complete_diff()
        diff_path = self.run_dir / "changes/final.diff"
        final_diff = self._store_diff(raw_diff, diff_path)

        files = self._changed_files()
        data = {
            "schema_version": SCHEMA_VERSION,
            "base_commit": self.base_commit,
            "files": files,
            "final_diff": {
                "path": "changes/final.diff",
                **final_diff,
            },
        }
        if self.queue_task:
            cumulative_path = self.run_dir / "changes/cumulative.diff"
            cumulative = self._store_diff(self._cumulative_diff(), cumulative_path)
            data["cumulative_diff"] = {
                "path": "changes/cumulative.diff",
                **cumulative,
            }
        cumulative_redaction_count = int(
            data.get("cumulative_diff", {}).get("redaction_count", 0)
            if isinstance(data.get("cumulative_diff"), Mapping)
            else 0
        )
        _atomic_write_json(self.run_dir / "changes/files.json", data)
        self.append(
            "diff.captured",
            {
                "path": "changes/final.diff",
                "raw_sha256": final_diff["raw_sha256"],
                "stored_sha256": final_diff["stored_sha256"],
                "redaction_count": final_diff["redaction_count"],
                "file_count": len(files),
                "cumulative_sha256": (
                    data.get("cumulative_diff", {}).get("raw_sha256")
                    if isinstance(data.get("cumulative_diff"), Mapping)
                    else None
                ),
            },
            redacted=bool(
                int(final_diff["redaction_count"]) + cumulative_redaction_count
            ),
        )
        return data

    @staticmethod
    def _store_diff(raw_diff: bytes, path: Path) -> dict[str, Any]:
        raw_sha = _sha256_bytes(raw_diff)
        raw_text = raw_diff.decode("utf-8", errors="replace")
        stored_text = redact_sensitive_text(raw_text)
        replacement_count = max(
            0,
            stored_text.count("[REDACTED]") - raw_text.count("[REDACTED]"),
        )
        if stored_text != raw_text and replacement_count == 0:
            replacement_count = 1
        _atomic_write_text(path, stored_text)
        return {
            "raw_sha256": raw_sha,
            "stored_sha256": _sha256_bytes(path.read_bytes()),
            "redaction_count": replacement_count,
        }

    def denied_event_count(self) -> int:
        if not self.events_path.is_file():
            return 0
        count = 0
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("type") == "permission.denied":
                count += 1
        return count

    def _record_codex_command(
        self, turn_number: int, item: Mapping[str, Any]
    ) -> None:
        command = item.get("command", "")
        action_commands = [
            action.get("command")
            for action in item.get("commandActions", [])
            if isinstance(action, Mapping) and action.get("command")
        ]
        classification: tuple[str, str] | None = None
        explicitly_forbidden = False
        denied_target: Any = command
        for candidate in [*action_commands, command]:
            classification = ExecutionPolicy.denied_command_classification(
                str(candidate) if not isinstance(candidate, list) else candidate
            )
            if classification is not None:
                explicitly_forbidden = True
            if classification is None:
                outside_target = self._outside_filesystem_target(candidate)
                if outside_target is not None:
                    classification = ("filesystem", "outside task workspace")
                    denied_target = outside_target
                    explicitly_forbidden = True
            if classification:
                if classification[0] != "filesystem":
                    denied_target = candidate
                break
        output = str(
            item.get("aggregatedOutput", item.get("aggregated_output", ""))
        )
        if classification is None and _looks_like_permission_failure(output):
            classification = ("filesystem", "filesystem policy denied command")
            denied_target = command
        log_index = len(list((self.run_dir / "logs/codex").glob("*.log"))) + 1
        log_path = (
            self.run_dir
            / "logs/codex"
            / f"turn-{turn_number:02d}-command-{log_index:02d}.log"
        )
        content = "\n".join(
            [
                f"command: {command}",
                f"cwd: {item.get('cwd', '')}",
                f"status: {item.get('status', '')}",
                f"exit_code: {item.get('exitCode', item.get('exit_code'))}",
                "",
                output,
            ]
        )
        _atomic_write_text(log_path, redact_sensitive_text(content))
        payload = {
            "command": command,
            "exit_code": item.get("exitCode", item.get("exit_code")),
            "duration_seconds": float(
                item.get("durationMs", item.get("duration_ms", 0)) or 0
            )
            / 1000,
            "log_path": log_path.relative_to(self.run_dir).as_posix(),
            "log_sha256": _sha256_bytes(log_path.read_bytes()),
        }
        self.append(
            "command.completed",
            payload,
            source="codex",
            turn_number=turn_number,
            redacted=True,
        )
        if classification:
            category, reason = classification
            # The permission profile should prevent the operation. Recording a
            # forbidden command makes any runtime regression visible.
            self.append(
                "permission.denied",
                {
                    "category": category,
                    "target": denied_target,
                    "reason": reason,
                    "exit_code": item.get("exitCode", item.get("exit_code")),
                },
                source="codex",
                turn_number=turn_number,
                redacted=True,
            )
            if (
                explicitly_forbidden
                and item.get("exitCode", item.get("exit_code")) == 0
            ):
                raise InfrastructureError(
                    "A command forbidden by the task policy unexpectedly succeeded"
                )

    def _outside_filesystem_target(self, command: str | list[str]) -> str | None:
        """Return an explicit task-forbidden path referenced by a command."""

        if isinstance(command, str):
            try:
                words = shlex.split(command)
            except ValueError:
                words = command.strip().split()
        else:
            words = [str(word) for word in command]
        if not words:
            return None

        executable = Path(words[0]).name.lower()
        if executable in {"bash", "sh", "zsh"} and len(words) >= 3:
            for index, word in enumerate(words[1:-1], start=1):
                if word in {"-c", "-lc"}:
                    return self._outside_filesystem_target(words[index + 1])

        lexical_worktree = Path(os.path.abspath(self.worktree))
        protected_inside = (
            lexical_worktree / ".git",
            lexical_worktree / ".codex-runtime/codex-home/auth.json",
        )
        allowed_system_roots = [
            Path(path)
            for path in (
                "/System",
                "/usr",
                "/bin",
                "/sbin",
                "/Library",
                "/private/etc",
                "/private/var/db/timezone",
                "/dev",
                "/opt",
            )
        ]
        conda_executable = os.environ.get("CONDA_EXE")
        if conda_executable:
            conda_path = Path(conda_executable).expanduser().resolve()
            if len(conda_path.parents) >= 2:
                allowed_system_roots.append(conda_path.parents[1])

        for raw_word in words[1:]:
            word = raw_word.strip()
            if (
                not word
                or word.startswith("-")
                or word in {"|", "||", "&&", ";", ">", ">>", "<", "2>"}
                or "://" in word
            ):
                continue
            looks_like_path = (
                word.startswith(("/", "~/", "../", "./"))
                or word in {".git", ".codex-runtime/codex-home/auth.json"}
                or word.startswith((".git/", ".codex-runtime/codex-home/auth.json/"))
            )
            if not looks_like_path:
                continue
            expanded = Path(word).expanduser()
            lexical = Path(
                os.path.abspath(
                    expanded
                    if expanded.is_absolute()
                    else lexical_worktree / expanded
                )
            )
            if any(_is_within(lexical, root) for root in protected_inside):
                return word
            if _is_within(lexical, lexical_worktree):
                continue
            if any(_is_within(lexical, root) for root in allowed_system_roots):
                continue
            return word
        return None

    def _record_file_change(
        self, turn_number: int, item: Mapping[str, Any]
    ) -> None:
        outside_paths: list[str] = []
        for change in item.get("changes", []):
            if not isinstance(change, Mapping) or not change.get("path"):
                continue
            raw_path = Path(str(change["path"])).expanduser()
            path = raw_path if raw_path.is_absolute() else self.worktree / raw_path
            try:
                path.resolve().relative_to(self.worktree)
            except ValueError:
                outside_paths.append(str(change["path"]))
        for path in outside_paths:
            self.append(
                "permission.denied",
                {
                    "category": "filesystem",
                    "target": path,
                    "reason": "outside task workspace",
                },
                source="codex",
                turn_number=turn_number,
                redacted=True,
            )
        self.append(
            "file.changed",
            {
                "item": item,
                "worktree_diff_sha256": self.current_diff_sha256(),
            },
            source="codex",
            turn_number=turn_number,
            redacted=True,
        )
        if outside_paths and item.get("status") == "completed":
            raise InfrastructureError(
                "Codex reported a completed file change outside the task worktree"
            )

    def _complete_diff(self) -> bytes:
        arguments = ["diff", "--binary", "--find-renames"]
        if not self.inherited_baseline:
            arguments.append(self.base_commit)
        arguments.append("--")
        tracked = self._git_bytes(*arguments)
        chunks = [tracked]
        for path in self._untracked_paths():
            completed = self._run_git(
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                path,
                allowed_exit_codes={0, 1},
            )
            chunks.append(completed.stdout)
        return b"".join(chunks)

    def _cumulative_diff(self) -> bytes:
        tracked = self._git_bytes(
            "diff", "--binary", "--find-renames", self.base_commit, "--"
        )
        chunks = [tracked]
        for path in self._untracked_paths():
            completed = self._run_git(
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                path,
                allowed_exit_codes={0, 1},
            )
            chunks.append(completed.stdout)
        return b"".join(chunks)

    def _changed_files(self) -> list[dict[str, Any]]:
        statuses = self._name_status()
        numstat = self._numstat()
        for path in self._untracked_paths():
            statuses.setdefault(path, "added")
        files: list[dict[str, Any]] = []
        for path, status in sorted(statuses.items()):
            before = self._git_file_at_base(path)
            target = self.worktree / path
            after = target.read_bytes() if target.is_file() else None
            additions, deletions = numstat.get(path, (0, 0))
            files.append(
                {
                    "path": path,
                    "status": status,
                    "before_sha256": _sha256_bytes(before) if before is not None else None,
                    "after_sha256": _sha256_bytes(after) if after is not None else None,
                    "additions": additions,
                    "deletions": deletions,
                }
            )
        return files

    def _name_status(self) -> dict[str, str]:
        arguments = ["diff", "--name-status", "-z", "--find-renames"]
        if not self.inherited_baseline:
            arguments.append(self.base_commit)
        arguments.append("--")
        raw = self._git_bytes(*arguments)
        parts = raw.decode("utf-8", errors="surrogateescape").split("\0")
        statuses: dict[str, str] = {}
        index = 0
        names = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
        while index < len(parts) and parts[index]:
            code = parts[index]
            index += 1
            if code.startswith(("R", "C")):
                if index + 1 >= len(parts):
                    break
                old_path, new_path = parts[index], parts[index + 1]
                statuses[old_path] = "deleted"
                statuses[new_path] = "renamed" if code.startswith("R") else "added"
                index += 2
            else:
                if index >= len(parts):
                    break
                path = parts[index]
                index += 1
                statuses[path] = names.get(code[:1], "modified")
        return statuses

    def _numstat(self) -> dict[str, tuple[int, int]]:
        arguments = ["diff", "--numstat", "--find-renames"]
        if not self.inherited_baseline:
            arguments.append(self.base_commit)
        arguments.append("--")
        text = self._git_text(*arguments)
        values: dict[str, tuple[int, int]] = {}
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            additions = int(parts[0]) if parts[0].isdigit() else 0
            deletions = int(parts[1]) if parts[1].isdigit() else 0
            values[parts[-1]] = (additions, deletions)
        return values

    def _untracked_paths(self) -> list[str]:
        raw = self._git_bytes("ls-files", "--others", "--exclude-standard", "-z")
        return sorted(
            path
            for path in raw.decode("utf-8", errors="surrogateescape").split("\0")
            if path and (self.worktree / path).is_file()
        )

    def _git_file_at_base(self, path: str) -> bytes | None:
        if self.inherited_baseline:
            completed = self._run_git(
                "show", f":{path}", allowed_exit_codes={0, 128}
            )
            return completed.stdout if completed.returncode == 0 else None
        completed = self._run_git(
            "show", f"{self.base_commit}:{path}", allowed_exit_codes={0, 128}
        )
        return completed.stdout if completed.returncode == 0 else None

    def _git_text(self, *arguments: str) -> str:
        return self._git_bytes(*arguments).decode("utf-8", errors="replace")

    def _git_bytes(self, *arguments: str) -> bytes:
        return self._run_git(*arguments).stdout

    def _run_git(
        self,
        *arguments: str,
        allowed_exit_codes: set[int] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        allowed = allowed_exit_codes or {0}
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.worktree), *arguments],
                capture_output=True,
                timeout=60,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise InfrastructureError(
                f"Unable to capture Git changes ({type(exc).__name__})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError("Git change capture timed out") from exc
        if completed.returncode not in allowed:
            detail = redact_sensitive_text(
                completed.stderr.decode("utf-8", errors="replace")
            )[:2_000]
            raise InfrastructureError(
                f"Git change capture failed (exit_code={completed.returncode}): {detail}"
            )
        return completed

    def _turn_dir(self, turn_number: int) -> Path:
        if turn_number < 1:
            raise ValueError("turn_number must be at least 1")
        return self.run_dir / "turns" / f"turn-{turn_number:02d}"

    def _completed_item_keys(self, turn_number: int) -> set[str]:
        keys: set[str] = set()
        for event in self._events():
            if event.get("type") != "codex.item.completed" or event.get(
                "turn_number"
            ) != turn_number:
                continue
            item = event.get("payload", {}).get("data", {}).get("item", {})
            item = _unwrap_item(item) if isinstance(item, Mapping) else {}
            if item:
                keys.add(_item_key(item))
        return keys

    def _relative_worktree_path(self, raw_path: str) -> str | None:
        path = Path(raw_path).expanduser()
        absolute = path if path.is_absolute() else self.worktree / path
        try:
            return absolute.resolve().relative_to(self.worktree).as_posix()
        except ValueError:
            return None

    def _last_event(
        self,
        event_type: str,
        *,
        turn_number: int | None = None,
        round_number: int | None = None,
    ) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        for event in self._events():
            if event.get("type") != event_type:
                continue
            if turn_number is not None and event.get("turn_number") != turn_number:
                continue
            if round_number is not None and event.get("round_number") != round_number:
                continue
            latest = event
        return latest

    def _events(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _load_next_seq(self) -> int:
        if not self.events_path.is_file():
            return 1
        last = 0
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                seq = int(event["seq"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise InfrastructureError("events.jsonl contains an invalid event") from exc
            if seq != last + 1:
                raise InfrastructureError("events.jsonl sequence is not continuous")
            last = seq
        return last + 1


def file_sha256(path: str | Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _sha256_bytes(value: bytes | None) -> str:
    return hashlib.sha256(value or b"").hexdigest()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value


def _item_type(item: Any) -> str:
    if not isinstance(item, Mapping):
        return "unknown"
    item = _unwrap_item(item)
    return str(item.get("type", "unknown"))


def _unwrap_item(item: Mapping[str, Any]) -> dict[str, Any]:
    if "root" in item and isinstance(item["root"], Mapping):
        item = item["root"]
    return {str(key): value for key, value in item.items()}


def _item_key(item: Mapping[str, Any]) -> str:
    if item.get("id"):
        return f"id:{item['id']}"
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return f"sha256:{_sha256_bytes(encoded.encode('utf-8'))}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _looks_like_permission_failure(output: str) -> bool:
    normalized = output.casefold()
    return any(
        marker in normalized
        for marker in (
            "operation not permitted",
            "permission denied",
            "sandbox denied",
            "not allowed by sandbox",
        )
    )


__all__ = ["AuditRecorder", "file_sha256"]
