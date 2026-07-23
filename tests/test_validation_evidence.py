from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from codex_loop.models import CommandResult, InfrastructureError, ValidationRound
from codex_loop.state import StateStore
from codex_loop.validation_evidence import ValidationEvidenceSnapshot


TASK_ID = "validation-evidence-task"
BASE_COMMIT = "a" * 40
FINAL_DIFF_SHA256 = "b" * 64


def _command_result(
    *,
    command: list[str] | None = None,
    exit_code: int | None = 0,
    timed_out: bool = False,
    infrastructure_error: str | None = None,
    stage: str = "targeted",
) -> CommandResult:
    return CommandResult(
        command=command or ["pytest", "-q", "tests/test_target.py"],
        cwd="/workspace",
        stage=stage,
        started_at="2026-07-22T10:00:00+08:00",
        duration_seconds=0.25,
        exit_code=exit_code,
        stdout="validation stdout",
        stderr="validation stderr" if exit_code else "",
        timed_out=timed_out,
        infrastructure_error=infrastructure_error,
    )


def _freeze_round(
    root: Path,
    results: list[CommandResult],
    *,
    passed: bool,
    round_infrastructure_error: str | None = None,
) -> tuple[StateStore, ValidationRound, ValidationEvidenceSnapshot]:
    store = StateStore(root)
    for command_index, result in enumerate(results, start=1):
        store.write_command_log(TASK_ID, 1, command_index, result)
    validation_round = ValidationRound(
        round_number=1,
        targeted_results=results,
        passed=passed,
        stage="targeted",
        started_at="2026-07-22T10:00:00+08:00",
        finished_at="2026-07-22T10:00:01+08:00",
        infrastructure_error=round_infrastructure_error,
    )
    snapshot = ValidationEvidenceSnapshot.from_round(
        validation_round,
        task_id=TASK_ID,
        base_commit=BASE_COMMIT,
        final_diff_sha256=FINAL_DIFF_SHA256,
        control_repo_root=root,
        run_dir=store.run_dir(TASK_ID),
    )
    return store, validation_round, snapshot


@pytest.mark.parametrize(
    (
        "result",
        "passed",
        "round_infrastructure_error",
        "expected_status",
        "expected_command_status",
    ),
    [
        (_command_result(), True, None, "pass", "pass"),
        (_command_result(exit_code=1), False, None, "fail", "fail"),
        (
            _command_result(exit_code=None, timed_out=True),
            False,
            None,
            "fail",
            "fail",
        ),
        (
            _command_result(
                exit_code=None,
                infrastructure_error="validation process could not start",
            ),
            False,
            "validation runtime unavailable",
            "infra_error",
            "infra_error",
        ),
    ],
    ids=["pass", "ordinary-failure", "timeout", "infrastructure-error"],
)
def test_snapshot_freezes_each_validation_outcome(
    tmp_path: Path,
    result: CommandResult,
    passed: bool,
    round_infrastructure_error: str | None,
    expected_status: str,
    expected_command_status: str,
) -> None:
    store, validation_round, snapshot = _freeze_round(
        tmp_path,
        [result],
        passed=passed,
        round_infrastructure_error=round_infrastructure_error,
    )

    assert snapshot.status == expected_status
    assert snapshot.commands[0].evidence_id == "VAL-001"
    assert snapshot.commands[0].status == expected_command_status
    assert snapshot.commands[0].timed_out is result.timed_out
    snapshot.verify_hash()
    snapshot.verify_binding(
        task_id=TASK_ID,
        validation_round=1,
        base_commit=BASE_COMMIT,
        final_diff_sha256=FINAL_DIFF_SHA256,
    )
    snapshot.verify_round(
        validation_round,
        control_repo_root=tmp_path,
        run_dir=store.run_dir(TASK_ID),
    )
    snapshot.verify_logs(store.run_dir(TASK_ID))


def test_legacy_snapshot_is_explicit_and_contains_no_fabricated_commands(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    snapshot = ValidationEvidenceSnapshot.legacy_unavailable(
        task_id=TASK_ID,
        validation_round=1,
        base_commit=BASE_COMMIT,
        final_diff_sha256=FINAL_DIFF_SHA256,
    )

    assert snapshot.status == "legacy_evidence_unavailable"
    assert snapshot.commands == []
    assert snapshot.round_infrastructure_error is None
    snapshot.verify_binding(
        task_id=TASK_ID,
        validation_round=1,
        base_commit=BASE_COMMIT,
        final_diff_sha256=FINAL_DIFF_SHA256,
    )
    snapshot.verify_logs(store.run_dir(TASK_ID))

    path = store.save_validation_evidence(TASK_ID, snapshot)
    assert path == store.validation_evidence_path(TASK_ID, 1)
    assert store.load_validation_evidence(TASK_ID, 1) == snapshot


@pytest.mark.parametrize("replacement_id", ["VAL-001", "VAL-003"])
def test_snapshot_rejects_duplicate_or_non_continuous_evidence_ids(
    tmp_path: Path,
    replacement_id: str,
) -> None:
    _, _, snapshot = _freeze_round(
        tmp_path,
        [_command_result(), _command_result(stage="full")],
        passed=True,
    )
    payload = snapshot.to_dict()
    payload["commands"][1]["evidence_id"] = replacement_id

    with pytest.raises(ValidationError, match="unique and continuous"):
        ValidationEvidenceSnapshot.model_validate(payload)


def test_snapshot_and_command_log_redact_credentials(tmp_path: Path) -> None:
    command_secret = "sk-1234567890abcdef"
    split_argument_secret = "opaque-credential-value"
    error_secret = "bearer-token-123456"
    round_secret = "database-password"
    result = _command_result(
        command=[
            "curl",
            "--token",
            split_argument_secret,
            f"--api-key={command_secret}",
        ],
        exit_code=None,
        infrastructure_error=f"Authorization: Bearer {error_secret}",
    )
    store, _, snapshot = _freeze_round(
        tmp_path,
        [result],
        passed=False,
        round_infrastructure_error=f"password={round_secret}",
    )

    serialized = json.dumps(snapshot.to_dict(), ensure_ascii=False)
    log_text = (store.run_dir(TASK_ID) / snapshot.commands[0].log_path).read_text(
        encoding="utf-8"
    )
    for secret in (
        command_secret,
        split_argument_secret,
        error_secret,
        round_secret,
    ):
        assert secret not in serialized
        assert secret not in log_text
    assert "[REDACTED]" in serialized
    assert "[REDACTED]" in log_text
    assert result.command == [
        "curl",
        "--token",
        "[REDACTED]",
        "--api-key=[REDACTED]",
    ]


def test_failed_round_redacts_split_argument_secret_from_summary(
    tmp_path: Path,
) -> None:
    secret = "opaque-failure-secret"
    store, validation_round, _snapshot = _freeze_round(
        tmp_path,
        [_command_result(command=["tool", "--token", secret], exit_code=1)],
        passed=False,
    )
    validation_round.failure_summary = f"tool --token {secret}: exit code 1"

    path = store.save_round(TASK_ID, validation_round)
    serialized = path.read_text(encoding="utf-8")

    assert secret not in serialized
    assert "tool --token [REDACTED]" in serialized
    assert store.save_round(TASK_ID, validation_round) == path


def test_loading_rejects_a_tampered_snapshot(tmp_path: Path) -> None:
    store, _, snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    path = store.save_validation_evidence(TASK_ID, snapshot)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-07-22T11:00:00+08:00"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(InfrastructureError, match="snapshot hash changed"):
        store.load_validation_evidence(TASK_ID, 1)


def test_snapshot_rejects_an_unsupported_schema_version(tmp_path: Path) -> None:
    _, _, snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    payload = snapshot.to_dict()
    payload["schema_version"] = 99

    with pytest.raises(ValidationError, match="Input should be 1"):
        ValidationEvidenceSnapshot.model_validate(payload)


def test_round_verification_binds_the_exact_log_path(tmp_path: Path) -> None:
    store, validation_round, snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    original = store.run_dir(TASK_ID) / snapshot.commands[0].log_path
    replacement = original.with_name("same-content.log")
    replacement.write_bytes(original.read_bytes())
    validation_round.command_results[0].log_path = replacement.relative_to(
        tmp_path
    ).as_posix()

    with pytest.raises(InfrastructureError, match="command metadata changed"):
        snapshot.verify_round(
            validation_round,
            control_repo_root=tmp_path,
            run_dir=store.run_dir(TASK_ID),
        )


def test_log_verification_rejects_a_missing_log(tmp_path: Path) -> None:
    store, _, snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    log_path = store.run_dir(TASK_ID) / snapshot.commands[0].log_path
    log_path.unlink()

    with pytest.raises(InfrastructureError, match=r"log is missing: VAL-001"):
        snapshot.verify_logs(store.run_dir(TASK_ID))


def test_log_verification_rejects_changed_log_content(tmp_path: Path) -> None:
    store, _, snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    log_path = store.run_dir(TASK_ID) / snapshot.commands[0].log_path
    log_path.write_text(
        log_path.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(InfrastructureError, match=r"log hash changed: VAL-001"):
        snapshot.verify_logs(store.run_dir(TASK_ID))


def test_state_store_saves_evidence_idempotently_but_rejects_replacement(
    tmp_path: Path,
) -> None:
    store, _, snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    path = store.save_validation_evidence(TASK_ID, snapshot)

    assert store.save_validation_evidence(TASK_ID, snapshot) == path
    assert store.load_validation_evidence(TASK_ID, 1) == snapshot

    replacement = ValidationEvidenceSnapshot.legacy_unavailable(
        task_id=TASK_ID,
        validation_round=1,
        base_commit=BASE_COMMIT,
        final_diff_sha256=FINAL_DIFF_SHA256,
    )
    with pytest.raises(InfrastructureError, match="immutable once frozen"):
        store.save_validation_evidence(TASK_ID, replacement)

    with pytest.raises(ValueError, match="task_id does not match"):
        store.save_validation_evidence("another-task", snapshot)


def test_snapshot_bounds_command_count_and_argument_length(tmp_path: Path) -> None:
    _, _, snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    payload = snapshot.to_dict()
    command = payload["commands"][0]
    payload["commands"] = [
        {**command, "evidence_id": f"VAL-{index:03d}"} for index in range(1, 102)
    ]
    with pytest.raises(ValidationError, match="at most 100"):
        ValidationEvidenceSnapshot.model_validate(payload)

    with pytest.raises(ValidationError, match="at most 2000"):
        _freeze_round(
            tmp_path / "long-argument",
            [_command_result(command=["x" * 2001])],
            passed=True,
        )


def test_state_store_does_not_overwrite_a_frozen_validation_round(
    tmp_path: Path,
) -> None:
    store, validation_round, _snapshot = _freeze_round(
        tmp_path,
        [_command_result()],
        passed=True,
    )
    path = store.save_round(TASK_ID, validation_round)
    assert store.save_round(TASK_ID, validation_round) == path

    validation_round.failure_summary = "replacement"
    with pytest.raises(InfrastructureError, match="immutable once persisted"):
        store.save_round(TASK_ID, validation_round)
