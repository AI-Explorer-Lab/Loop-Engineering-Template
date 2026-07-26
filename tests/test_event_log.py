from __future__ import annotations

import fcntl
import json
from multiprocessing import get_context
import os
from pathlib import Path
from threading import Thread

import pytest

from codex_loop.audit import AuditRecorder
from codex_loop.event_log import (
    append_event,
    inspect_event_log,
    persist_integrity_checkpoint,
    read_events,
)
from codex_loop.models import InfrastructureError
from codex_loop.state import StateStore


def _event(seq: int, label: str) -> dict[str, object]:
    return {"schema_version": 1, "seq": seq, "type": label}


def _process_append(path: str, prefix: str, count: int) -> None:
    for index in range(count):
        append_event(
            path,
            lambda seq, current=index: _event(seq, f"{prefix}-{current}"),
        )


def _hold_file_lock(path: str, ready: object, release: object) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        release.wait(timeout=5)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_two_long_lived_writers_allocate_fresh_sequences(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    first = append_event(path, lambda seq: _event(seq, "first"))
    second = append_event(path, lambda seq: _event(seq, "second"))

    assert [first["seq"], second["seq"]] == [1, 2]
    events, inspection = read_events(path)
    assert [item["seq"] for item in events] == [1, 2]
    assert inspection.valid


def test_two_long_lived_audit_recorders_revalidate_before_each_append(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    first = AuditRecorder(run_dir, tmp_path, "base")
    second = AuditRecorder(run_dir, tmp_path, "base")

    first_event = first.append("first")
    second_event = second.append("second")
    third_event = first.append("third")

    assert [first_event.seq, second_event.seq, third_event.seq] == [1, 2, 3]
    assert inspect_event_log(run_dir / "events.jsonl").valid


def test_threads_share_one_continuous_sequence(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"

    threads = [
        Thread(
            target=lambda prefix=index: [
                append_event(
                    path,
                    lambda seq, item=item, prefix=prefix: _event(
                        seq, f"{prefix}-{item}"
                    ),
                )
                for item in range(20)
            ]
        )
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    events, inspection = read_events(path)
    assert inspection.valid
    assert [item["seq"] for item in events] == list(range(1, 81))


def test_audit_and_state_store_share_one_sequence_allocator(tmp_path: Path) -> None:
    task_id = "mixed-writers"
    store = StateStore(tmp_path)
    run_dir = store.run_dir(task_id)
    audit = AuditRecorder(run_dir, tmp_path, "base")
    threads = [
        Thread(
            target=lambda: [
                audit.append("audit.writer", {"index": index}) for index in range(20)
            ]
        ),
        Thread(
            target=lambda: [
                store.append_event(task_id, "state.writer", {"index": index})
                for index in range(20)
            ]
        ),
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    events, inspection = read_events(run_dir / "events.jsonl")
    assert inspection.valid
    assert [item["seq"] for item in events] == list(range(1, 41))
    assert {item["type"] for item in events} == {"audit.writer", "state.writer"}


def test_processes_share_one_continuous_sequence(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    context = get_context("spawn")
    processes = [
        context.Process(target=_process_append, args=(str(path), f"p{index}", 15))
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0]
    events, inspection = read_events(path)
    assert inspection.valid
    assert [item["seq"] for item in events] == list(range(1, 31))


def test_lock_timeout_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    context = get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_file_lock,
        args=(str(path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(timeout=5)
        with pytest.raises(InfrastructureError, match="lock timed out"):
            append_event(
                path,
                lambda seq: _event(seq, "blocked"),
                timeout_seconds=0.05,
            )
    finally:
        release.set()
        process.join(timeout=5)
    assert process.exitcode == 0
    assert path.read_bytes() == b""


@pytest.mark.parametrize(
    ("content", "issue_type"),
    [
        (
            b'{"seq": 1, "type": "one"}\n{"seq": 1, "type": "duplicate"}\n',
            "duplicate_sequence",
        ),
        (
            b'{"seq": 1, "type": "one"}\n{"seq": 3, "type": "gap"}\n',
            "gap",
        ),
        (
            b'{"seq": 2, "type": "ahead"}\n{"seq": 1, "type": "back"}\n',
            "gap",
        ),
        (b'{"seq": 1, "type": ', "truncated_line"),
        (b'{"seq": 1, "type": invalid}\n', "invalid_json"),
        (b"[]\n", "non_object"),
        (b"\n", "blank_line"),
        (b'{"type": "missing"}\n', "missing_seq"),
    ],
)
def test_invalid_logs_refuse_append_without_mutation(
    tmp_path: Path,
    content: bytes,
    issue_type: str,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(content)

    inspection = inspect_event_log(path)
    assert not inspection.valid
    assert any(item["type"] == issue_type for item in inspection.issues)
    with pytest.raises(InfrastructureError, match="integrity check failed"):
        append_event(path, lambda seq: _event(seq, "must-not-write"))

    assert path.read_bytes() == content


def test_valid_legacy_log_can_continue(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_event(1, "legacy-one")),
                json.dumps(_event(2, "legacy-two")),
                "",
            ]
        ),
        encoding="utf-8",
    )

    appended = append_event(path, lambda seq: _event(seq, "new"))

    assert appended["seq"] == 3
    assert inspect_event_log(path).valid


def test_checkpoint_binds_stable_event_bytes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    audit_dir = tmp_path / "audit"
    append_event(path, lambda seq: _event(seq, "one"))

    snapshot = persist_integrity_checkpoint(
        path,
        audit_dir=audit_dir,
        checkpoint="run-finalized",
        bindings={"diff_sha256": "a" * 64},
    )

    assert snapshot["status"] == "valid"
    assert snapshot["last_seq"] == 1
    assert snapshot["bindings"]["diff_sha256"] == "a" * 64
    checkpoint = tmp_path / snapshot["checkpoint_path"]
    assert checkpoint.is_file()
    latest = json.loads(
        (audit_dir / "event-log-integrity.json").read_text(encoding="utf-8")
    )
    assert latest["events_sha256"] == path_sha256(path)


def test_audit_checkpoint_persists_invalid_status_and_fails_closed(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = run_dir / "events.jsonl"
    path.write_text(
        '{"seq": 1, "type": "one"}\n{"seq": 1, "type": "duplicate"}\n',
        encoding="utf-8",
    )
    audit = object.__new__(AuditRecorder)
    audit.run_dir = run_dir
    audit.events_path = path

    with pytest.raises(InfrastructureError, match="checkpoint failed"):
        audit.checkpoint("run-finalized")

    latest = json.loads(
        (run_dir / "audit/event-log-integrity.json").read_text(encoding="utf-8")
    )
    assert latest["status"] == "invalid"
    assert latest["issues"][0]["type"] == "duplicate_sequence"


def path_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
