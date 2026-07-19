"""Project-local medium-term summaries and deterministic offline recall."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any, Mapping

from .models import utc_now_iso
from .state import redact_sensitive_data


TOKEN_PATTERN = re.compile(r"[\w.+#/-]+", re.UNICODE)


def _normalize(value: str) -> str:
    return " ".join(
        TOKEN_PATTERN.findall(unicodedata.normalize("NFKC", value).casefold())
    )


def _alias_groups(query: str, aliases: Mapping[str, list[str]]) -> list[set[str]]:
    groups: list[set[str]] = []
    consumed: set[str] = set()
    alias_map = {
        _normalize(source): {_normalize(source), *(_normalize(item) for item in values)}
        for source, values in aliases.items()
    }
    for token in _normalize(query).split():
        if not token or token in consumed:
            continue
        group = next((values for values in alias_map.values() if token in values), {token})
        group = {item for item in group if item}
        consumed.update(group)
        groups.append(group)
    return groups


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        redact_sensitive_data(dict(value)), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


class MediumTermMemory:
    """Append summaries and recall only human-approved, committed work."""

    def __init__(self, repo_root: str | Path, *, aliases: Mapping[str, list[str]] | None = None) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.root = self.repo_root / ".codex-orchestrator" / "memory"
        self.summaries_root = self.root / "run_summaries"
        self.index_path = self.root / "index.jsonl"
        self.lock_path = self.root / ".memory.lock"
        self.aliases = dict(aliases or {})

    def write_summary(self, summary: Mapping[str, Any]) -> dict[str, Any]:
        value = self._validate_summary(summary)
        path = self.summaries_root / f"{value['task_id']}.json"
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if path.is_file():
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    if existing != value:
                        raise ValueError("run summary is immutable once written")
                    return existing
                _atomic_json(path, value)
                record = {
                    "schema_version": 1,
                    "task_id": value["task_id"],
                    "commit_sha": value.get("commit_sha", ""),
                    "committed_at": value.get("committed_at", ""),
                    "review_status": value.get("review_status", ""),
                    "delivery_status": value.get("delivery_status", ""),
                    "summary_path": path.relative_to(self.repo_root).as_posix(),
                    "summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "indexed_at": utc_now_iso(),
                }
                line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                descriptor = os.open(
                    self.index_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                try:
                    os.write(descriptor, line.encode("utf-8"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return value
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def recall(
        self,
        *,
        query: str,
        tags: list[str] | None = None,
        paths: list[str] | None = None,
        technologies: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not self.summaries_root.is_dir():
            return []
        query_groups = _alias_groups(query, self.aliases)
        requested_tags = {_normalize(item) for item in tags or [] if _normalize(item)}
        requested_paths = {_normalize(item).split("/")[0] for item in paths or [] if _normalize(item)}
        requested_tech = {_normalize(item) for item in technologies or [] if _normalize(item)}
        results: list[dict[str, Any]] = []
        for path in sorted(self.summaries_root.glob("*.json")):
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if summary.get("review_status") != "approved" or summary.get(
                "delivery_status"
            ) not in {"committed", "archive_pending", "archived"}:
                continue
            text = _normalize(
                " ".join(
                    [
                        str(summary.get("requirement", "")),
                        str(summary.get("summary", "")),
                    ]
                )
            )
            k = min(10, sum(any(term in text for term in group) for group in query_groups))
            summary_tags = {_normalize(item) for item in summary.get("tags", [])}
            t = min(5, len(summary_tags & requested_tags))
            summary_paths = {
                _normalize(item).split("/")[0]
                for item in summary.get("paths", [])
                if _normalize(item)
            }
            p = min(5, len(summary_paths & requested_paths))
            summary_tech = {
                _normalize(item) for item in summary.get("technologies", [])
            }
            f = min(5, len(summary_tech & requested_tech))
            score = 4 * k + 3 * t + 2 * p + f
            if score <= 0 and query_groups:
                continue
            results.append(
                {
                    "task_id": str(summary["task_id"]),
                    "commit_sha": str(summary.get("commit_sha", "")),
                    "committed_at": str(summary.get("committed_at", "")),
                    "summary": str(summary.get("summary", "")),
                    "tags": list(summary.get("tags", [])),
                    "paths": list(summary.get("paths", [])),
                    "technologies": list(summary.get("technologies", [])),
                    "match_score": score,
                    "score_breakdown": {"K": k, "T": t, "P": p, "F": f},
                    "summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        results.sort(key=lambda item: str(item["task_id"]))
        results.sort(key=lambda item: str(item["committed_at"]), reverse=True)
        results.sort(key=lambda item: int(item["match_score"]), reverse=True)
        return results[: max(0, int(limit))]

    @staticmethod
    def _validate_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
        value = redact_sensitive_data(dict(summary))
        task_id = str(value.get("task_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
            raise ValueError("summary task_id is unsafe")
        value.setdefault("schema_version", 1)
        value.setdefault("created_at", utc_now_iso())
        value.setdefault("review_status", "pending")
        value.setdefault("delivery_status", "not_ready")
        value.setdefault("commit_sha", "")
        value.setdefault("committed_at", "")
        value.setdefault("requirement", "")
        value.setdefault("summary", "")
        value.setdefault("tags", [])
        value.setdefault("paths", [])
        value.setdefault("technologies", [])
        for name in ("tags", "paths", "technologies"):
            if not isinstance(value[name], list):
                raise ValueError(f"summary {name} must be a list")
            value[name] = [str(item) for item in value[name]]
        return value


__all__ = ["MediumTermMemory"]
