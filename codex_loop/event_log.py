"""Concurrency-safe ordered JSONL event logs and integrity checkpoints."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock, RLock
import time
from typing import Any, Callable, Iterator, Mapping

from .models import InfrastructureError, utc_now_iso


DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_PATH_LOCKS: dict[str, RLock] = {}
_PATH_LOCKS_GUARD = Lock()
_CHECKPOINT_NAME = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class EventLogInspection:
    """One structural inspection of an ordered JSONL event stream."""

    status: str
    event_count: int
    first_seq: int | None
    last_seq: int | None
    events_sha256: str
    issues: tuple[dict[str, Any], ...] = ()

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "event_count": self.event_count,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "events_sha256": self.events_sha256,
            "issue_count": len(self.issues),
            "issues": [dict(item) for item in self.issues],
        }


def append_event(
    path: str | Path,
    event_factory: Callable[[int], Mapping[str, Any]],
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Append one event after allocating its sequence under both locks."""

    resolved = Path(path).resolve()
    with _locked_descriptor(
        resolved,
        exclusive=True,
        create=True,
        timeout_seconds=timeout_seconds,
    ) as descriptor:
        events, inspection = _read_and_inspect_descriptor(descriptor)
        _raise_if_invalid(resolved, inspection)
        next_seq = int(inspection.last_seq or 0) + 1
        event = _build_event(event_factory, next_seq)
        _append_encoded_event(descriptor, event)
        _verify_after_append(descriptor, resolved, expected_seq=next_seq)
        return event


def append_event_once(
    path: str | Path,
    event_factory: Callable[[int], Mapping[str, Any]],
    matches: Callable[[Mapping[str, Any]], bool],
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], bool]:
    """Atomically return a matching event or append a new one."""

    resolved = Path(path).resolve()
    with _locked_descriptor(
        resolved,
        exclusive=True,
        create=True,
        timeout_seconds=timeout_seconds,
    ) as descriptor:
        events, inspection = _read_and_inspect_descriptor(descriptor)
        _raise_if_invalid(resolved, inspection)
        for event in events:
            if matches(event):
                return dict(event), False
        next_seq = int(inspection.last_seq or 0) + 1
        event = _build_event(event_factory, next_seq)
        _append_encoded_event(descriptor, event)
        _verify_after_append(descriptor, resolved, expected_seq=next_seq)
        return event, True


def read_events(
    path: str | Path,
    *,
    strict: bool = True,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], EventLogInspection]:
    """Read one stable event-log view while holding a shared lock."""

    resolved = Path(path).resolve()
    if not resolved.is_file():
        return [], _inspect_bytes(b"")
    with _locked_descriptor(
        resolved,
        exclusive=False,
        create=False,
        timeout_seconds=timeout_seconds,
    ) as descriptor:
        events, inspection = _read_and_inspect_descriptor(descriptor)
    if strict:
        _raise_if_invalid(resolved, inspection)
    return events, inspection


def inspect_event_log(
    path: str | Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> EventLogInspection:
    """Inspect without modifying the event stream."""

    return read_events(
        path,
        strict=False,
        timeout_seconds=timeout_seconds,
    )[1]


def persist_integrity_checkpoint(
    events_path: str | Path,
    *,
    audit_dir: str | Path,
    checkpoint: str,
    bindings: Mapping[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Persist an immutable valid checkpoint and an atomic latest projection."""

    resolved = Path(events_path).resolve()
    target_audit_dir = Path(audit_dir).resolve()
    if not resolved.is_file():
        inspection = _inspect_bytes(b"")
        events: list[dict[str, Any]] = []
    else:
        with _locked_descriptor(
            resolved,
            exclusive=False,
            create=False,
            timeout_seconds=timeout_seconds,
        ) as descriptor:
            events, inspection = _read_and_inspect_descriptor(descriptor)
            snapshot = _checkpoint_value(
                resolved,
                checkpoint=checkpoint,
                inspection=inspection,
                bindings=bindings,
            )
            persisted = _persist_checkpoint_locked(target_audit_dir, snapshot)
            if inspection.valid:
                return persisted
    del events
    snapshot = _checkpoint_value(
        resolved,
        checkpoint=checkpoint,
        inspection=inspection,
        bindings=bindings,
    )
    return _persist_checkpoint_locked(target_audit_dir, snapshot)


def load_latest_integrity(audit_dir: str | Path) -> dict[str, Any]:
    path = Path(audit_dir).resolve() / "event-log-integrity.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return dict(value) if isinstance(value, Mapping) else {}


def _checkpoint_value(
    events_path: Path,
    *,
    checkpoint: str,
    inspection: EventLogInspection,
    bindings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    value = {
        **inspection.to_dict(),
        "checkpoint": str(checkpoint),
        "events_path": events_path.name,
        "bindings": dict(bindings or {}),
        "checked_at": utc_now_iso(),
    }
    value["snapshot_sha256"] = _json_sha(value)
    return value


def _persist_checkpoint_locked(
    audit_dir: Path,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    audit_dir.mkdir(parents=True, exist_ok=True)
    latest_path = audit_dir / "event-log-integrity.json"
    value = dict(snapshot)
    if snapshot.get("status") == "valid":
        checkpoint_dir = audit_dir / "integrity-checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_name = _safe_checkpoint_name(str(snapshot.get("checkpoint", "")))
        last_seq = int(snapshot.get("last_seq") or 0)
        checkpoint_path = checkpoint_dir / f"{last_seq:06d}-{checkpoint_name}.json"
        if checkpoint_path.is_file():
            existing = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(existing, Mapping):
                raise InfrastructureError(
                    f"audit integrity checkpoint is invalid: {checkpoint_path}"
                )
            value = dict(existing)
        else:
            _atomic_write_json(checkpoint_path, value)
        value["checkpoint_path"] = checkpoint_path.relative_to(
            audit_dir.parent
        ).as_posix()
    _atomic_write_json(latest_path, value)
    return value


def _safe_checkpoint_name(value: str) -> str:
    normalized = _CHECKPOINT_NAME.sub("-", value.strip().casefold()).strip("-")
    return normalized or "checkpoint"


def _build_event(
    event_factory: Callable[[int], Mapping[str, Any]],
    seq: int,
) -> dict[str, Any]:
    value = event_factory(seq)
    if not isinstance(value, Mapping):
        raise TypeError("event factory must return an object")
    event = dict(value)
    if event.get("seq") != seq:
        raise InfrastructureError("event factory returned an inconsistent sequence")
    return event


def _append_encoded_event(descriptor: int, event: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_END)
    view = memoryview(encoded)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise InfrastructureError("events.jsonl append wrote zero bytes")
        view = view[written:]
    os.fsync(descriptor)


def _verify_after_append(descriptor: int, path: Path, *, expected_seq: int) -> None:
    _events, inspection = _read_and_inspect_descriptor(descriptor)
    _raise_if_invalid(path, inspection)
    if inspection.last_seq != expected_seq:
        raise InfrastructureError(
            "events.jsonl append did not persist the expected event"
        )


def _read_and_inspect_descriptor(
    descriptor: int,
) -> tuple[list[dict[str, Any]], EventLogInspection]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return _inspect_bytes_with_events(b"".join(chunks))


def _inspect_bytes(value: bytes) -> EventLogInspection:
    return _inspect_bytes_with_events(value)[1]


def _inspect_bytes_with_events(
    value: bytes,
) -> tuple[list[dict[str, Any]], EventLogInspection]:
    digest = hashlib.sha256(value).hexdigest()
    if not value:
        return [], EventLogInspection("valid", 0, None, None, digest)

    issues: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if not value.endswith(b"\n"):
        issues.append(
            {
                "line": len(value.splitlines()) or 1,
                "type": "truncated_line",
                "expected": "newline-terminated JSON object",
                "actual": "missing final newline",
            }
        )

    expected_seq = 1
    seen: set[int] = set()
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        if not raw_line.strip():
            issues.append(
                {
                    "line": line_number,
                    "type": "blank_line",
                    "expected": expected_seq,
                    "actual": "blank",
                }
            )
            continue
        try:
            decoded = raw_line.decode("utf-8")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            issues.append(
                {
                    "line": line_number,
                    "type": "invalid_json",
                    "expected": expected_seq,
                    "actual": type(exc).__name__,
                }
            )
            continue
        if not isinstance(parsed, Mapping):
            issues.append(
                {
                    "line": line_number,
                    "type": "non_object",
                    "expected": expected_seq,
                    "actual": type(parsed).__name__,
                }
            )
            continue
        event = dict(parsed)
        events.append(event)
        raw_seq = event.get("seq")
        if isinstance(raw_seq, bool) or not isinstance(raw_seq, int) or raw_seq < 1:
            issues.append(
                {
                    "line": line_number,
                    "type": "missing_seq",
                    "expected": expected_seq,
                    "actual": raw_seq,
                }
            )
            continue
        if raw_seq != expected_seq:
            if raw_seq in seen:
                issue_type = "duplicate_sequence"
            elif raw_seq < expected_seq:
                issue_type = "out_of_order"
            else:
                issue_type = "gap"
            issues.append(
                {
                    "line": line_number,
                    "type": issue_type,
                    "expected": expected_seq,
                    "actual": raw_seq,
                }
            )
        seen.add(raw_seq)
        expected_seq = raw_seq + 1

    valid_sequences = [
        event.get("seq")
        for event in events
        if isinstance(event.get("seq"), int) and not isinstance(event.get("seq"), bool)
    ]
    inspection = EventLogInspection(
        status="invalid" if issues else "valid",
        event_count=len(events),
        first_seq=(int(valid_sequences[0]) if valid_sequences else None),
        last_seq=(int(valid_sequences[-1]) if valid_sequences else None),
        events_sha256=digest,
        issues=tuple(issues),
    )
    return events, inspection


def _raise_if_invalid(path: Path, inspection: EventLogInspection) -> None:
    if inspection.valid:
        return
    first = inspection.issues[0] if inspection.issues else {}
    raise InfrastructureError(
        "events.jsonl integrity check failed "
        f"(path={path}, issue_count={len(inspection.issues)}, "
        f"first_issue={first.get('type', 'unknown')}, "
        f"line={first.get('line', 'unknown')})"
    )


def _path_lock(path: Path) -> RLock:
    key = str(path)
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, RLock())


@contextmanager
def _locked_descriptor(
    path: Path,
    *,
    exclusive: bool,
    create: bool,
    timeout_seconds: float,
) -> Iterator[int]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _path_lock(path)
    if not process_lock.acquire(timeout=timeout_seconds):
        raise InfrastructureError(f"events.jsonl lock timed out: {path}")
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_APPEND if exclusive or create else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        descriptor = os.open(path, flags, 0o600)
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise InfrastructureError(
                        f"events.jsonl file lock timed out: {path}"
                    ) from exc
                time.sleep(0.01)
        try:
            yield descriptor
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        process_lock.release()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        encoded = (
            json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _json_sha(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EventLogInspection",
    "append_event",
    "append_event_once",
    "inspect_event_log",
    "load_latest_integrity",
    "persist_integrity_checkpoint",
    "read_events",
]
