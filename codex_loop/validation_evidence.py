"""Frozen, bounded evidence produced by trusted validation commands."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import InfrastructureError, ValidationRound, utc_now_iso
from .state import (
    _atomic_write_json,
    redact_sensitive_arguments,
    redact_sensitive_text,
)


ValidationEvidenceStatus = Literal[
    "pass", "fail", "infra_error", "legacy_evidence_unavailable"
]


class ValidationCommandEvidence(BaseModel):
    """One command fact an evaluator may cite but never rewrite."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^VAL-[0-9]{3}$")
    stage: str = Field(min_length=1, max_length=100)
    command: list[Annotated[str, Field(max_length=2000)]] = Field(
        min_length=1,
        max_length=100,
    )
    status: Literal["pass", "fail", "infra_error"]
    exit_code: int | None
    duration_seconds: float = Field(ge=0)
    timed_out: bool
    infrastructure_error: str | None = Field(max_length=4000)
    log_path: str = Field(min_length=1, max_length=2000)
    log_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_command_status(self) -> "ValidationCommandEvidence":
        if self.status == "pass" and (
            self.exit_code != 0 or self.timed_out or self.infrastructure_error
        ):
            raise ValueError("passing validation evidence conflicts with command facts")
        if self.status == "infra_error" and not self.infrastructure_error:
            raise ValueError("infra_error evidence requires an infrastructure error")
        if self.status == "fail" and self.infrastructure_error:
            raise ValueError(
                "infrastructure errors cannot be classified as ordinary failure"
            )
        path = Path(self.log_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "validation evidence log_path must stay inside the run directory"
            )
        return self


class ValidationEvidenceSnapshot(BaseModel):
    """Tamper-evident projection of one validation round."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=200)
    validation_round: int = Field(ge=1)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    final_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ValidationEvidenceStatus
    commands: list[ValidationCommandEvidence] = Field(
        default_factory=list, max_length=100
    )
    round_infrastructure_error: str | None = Field(default=None, max_length=4000)
    created_at: str = Field(default_factory=utc_now_iso)
    snapshot_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot_status(self) -> "ValidationEvidenceSnapshot":
        if self.status == "legacy_evidence_unavailable":
            if self.commands or self.round_infrastructure_error:
                raise ValueError(
                    "legacy evidence cannot contain fabricated command facts"
                )
            return self
        serialized_size = len(
            json.dumps(
                self.to_dict(include_hash=False),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if serialized_size > 256_000:
            raise ValueError("validation evidence snapshot exceeds its size budget")
        if not self.commands:
            raise ValueError("validation evidence must contain at least one command")
        expected_ids = [
            f"VAL-{index:03d}" for index in range(1, len(self.commands) + 1)
        ]
        actual_ids = [item.evidence_id for item in self.commands]
        if actual_ids != expected_ids:
            raise ValueError("validation evidence ids must be unique and continuous")
        command_statuses = {item.status for item in self.commands}
        expected_status: ValidationEvidenceStatus = (
            "infra_error"
            if self.round_infrastructure_error or "infra_error" in command_statuses
            else "fail"
            if "fail" in command_statuses
            else "pass"
        )
        if self.status != expected_status:
            raise ValueError("validation evidence status does not match command facts")
        return self

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        if not include_hash:
            value.pop("snapshot_sha256", None)
        return value

    def verify_hash(self) -> None:
        if not self.snapshot_sha256 or _snapshot_sha256(self) != self.snapshot_sha256:
            raise InfrastructureError("validation evidence snapshot hash changed")

    def verify_binding(
        self,
        *,
        task_id: str,
        validation_round: int,
        base_commit: str,
        final_diff_sha256: str,
    ) -> None:
        self.verify_hash()
        expected = {
            "task_id": str(task_id),
            "validation_round": int(validation_round),
            "base_commit": str(base_commit),
            "final_diff_sha256": str(final_diff_sha256),
        }
        actual = {
            "task_id": self.task_id,
            "validation_round": self.validation_round,
            "base_commit": self.base_commit,
            "final_diff_sha256": self.final_diff_sha256,
        }
        if actual != expected:
            raise InfrastructureError(
                "validation evidence does not match task, round, base commit, or diff"
            )

    def verify_round(
        self,
        validation_round: ValidationRound,
        *,
        control_repo_root: str | Path,
        run_dir: str | Path,
    ) -> None:
        if self.status == "legacy_evidence_unavailable":
            return
        results = validation_round.command_results
        if validation_round.round_number != self.validation_round or len(
            results
        ) != len(self.commands):
            raise InfrastructureError("validation evidence does not match saved round")
        for evidence, result in zip(self.commands, results, strict=True):
            expected_status: Literal["pass", "fail", "infra_error"] = (
                "infra_error"
                if result.infrastructure_error
                else "pass"
                if result.passed
                else "fail"
            )
            if (
                evidence.stage
                != str(result.stage or validation_round.stage or "validation")
                or evidence.command != redact_sensitive_arguments(result.command)
                or evidence.status != expected_status
                or evidence.exit_code != result.exit_code
                or evidence.duration_seconds != result.duration_seconds
                or evidence.timed_out != result.timed_out
                or evidence.infrastructure_error
                != (
                    None
                    if not result.infrastructure_error
                    else redact_sensitive_text(result.infrastructure_error)
                )
                or evidence.log_path
                != _relative_log_path(
                    result.log_path,
                    control_repo_root=control_repo_root,
                    run_dir=run_dir,
                )
                or evidence.log_sha256 != result.log_sha256
            ):
                raise InfrastructureError(
                    "validation evidence command metadata changed"
                )
        expected_round_error = (
            None
            if not validation_round.infrastructure_error
            else redact_sensitive_text(validation_round.infrastructure_error)
        )
        if self.round_infrastructure_error != expected_round_error:
            raise InfrastructureError(
                "validation evidence round infrastructure error changed"
            )
        if validation_round.passed != (self.status == "pass"):
            raise InfrastructureError(
                "validation round result conflicts with frozen evidence"
            )

    def verify_logs(self, run_dir: str | Path) -> None:
        if self.status == "legacy_evidence_unavailable":
            return
        root = Path(run_dir).resolve()
        for command in self.commands:
            path = (root / command.log_path).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise InfrastructureError(
                    "validation evidence log path escapes the run directory"
                ) from exc
            if not path.is_file():
                raise InfrastructureError(
                    f"validation evidence log is missing: {command.evidence_id}"
                )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != command.log_sha256:
                raise InfrastructureError(
                    f"validation evidence log hash changed: {command.evidence_id}"
                )

    @classmethod
    def from_round(
        cls,
        validation_round: ValidationRound,
        *,
        task_id: str,
        base_commit: str,
        final_diff_sha256: str,
        control_repo_root: str | Path,
        run_dir: str | Path,
    ) -> "ValidationEvidenceSnapshot":
        artifact_root = Path(run_dir).resolve()
        commands: list[ValidationCommandEvidence] = []
        for index, result in enumerate(validation_round.command_results, start=1):
            if not result.log_path or not result.log_sha256:
                raise InfrastructureError(
                    "validation evidence requires persisted command logs"
                )
            relative_log = _relative_log_path(
                result.log_path,
                control_repo_root=control_repo_root,
                run_dir=artifact_root,
            )
            absolute_log = (artifact_root / relative_log).resolve()
            if not absolute_log.is_file():
                raise InfrastructureError("validation command log is missing")
            actual_log_sha256 = hashlib.sha256(absolute_log.read_bytes()).hexdigest()
            if actual_log_sha256 != result.log_sha256:
                raise InfrastructureError("validation command log hash changed")
            status: Literal["pass", "fail", "infra_error"] = (
                "infra_error"
                if result.infrastructure_error
                else "pass"
                if result.passed
                else "fail"
            )
            commands.append(
                ValidationCommandEvidence(
                    evidence_id=f"VAL-{index:03d}",
                    stage=str(result.stage or validation_round.stage or "validation"),
                    command=redact_sensitive_arguments(result.command),
                    status=status,
                    exit_code=result.exit_code,
                    duration_seconds=result.duration_seconds,
                    timed_out=result.timed_out,
                    infrastructure_error=(
                        None
                        if not result.infrastructure_error
                        else redact_sensitive_text(result.infrastructure_error)
                    ),
                    log_path=relative_log,
                    log_sha256=result.log_sha256,
                )
            )
        if not commands:
            raise InfrastructureError("validation round produced no command evidence")
        statuses = {item.status for item in commands}
        status: ValidationEvidenceStatus = (
            "infra_error"
            if validation_round.infrastructure_error or "infra_error" in statuses
            else "fail"
            if "fail" in statuses
            else "pass"
        )
        if validation_round.passed != (status == "pass"):
            raise InfrastructureError(
                "validation round result conflicts with persisted command evidence"
            )
        snapshot = cls(
            task_id=str(task_id),
            validation_round=validation_round.round_number,
            base_commit=str(base_commit),
            final_diff_sha256=str(final_diff_sha256),
            status=status,
            commands=commands,
            round_infrastructure_error=(
                None
                if not validation_round.infrastructure_error
                else redact_sensitive_text(validation_round.infrastructure_error)
            ),
        )
        return cls.model_validate(
            {
                **snapshot.to_dict(include_hash=False),
                "snapshot_sha256": _snapshot_sha256(snapshot),
            }
        )

    @classmethod
    def legacy_unavailable(
        cls,
        *,
        task_id: str,
        validation_round: int,
        base_commit: str,
        final_diff_sha256: str,
    ) -> "ValidationEvidenceSnapshot":
        snapshot = cls(
            task_id=str(task_id),
            validation_round=int(validation_round),
            base_commit=str(base_commit),
            final_diff_sha256=str(final_diff_sha256),
            status="legacy_evidence_unavailable",
            commands=[],
        )
        return cls.model_validate(
            {
                **snapshot.to_dict(include_hash=False),
                "snapshot_sha256": _snapshot_sha256(snapshot),
            }
        )

    @classmethod
    def load(cls, path: str | Path) -> "ValidationEvidenceSnapshot":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise InfrastructureError("validation evidence must contain one object")
        snapshot = cls.model_validate(value)
        snapshot.verify_hash()
        return snapshot

    def save(self, path: str | Path) -> Path:
        self.verify_hash()
        target = Path(path)
        if target.is_file():
            existing = self.load(target)
            if existing.to_dict() != self.to_dict():
                raise InfrastructureError(
                    "validation evidence is immutable once frozen"
                )
            return target
        _atomic_write_json(target, self.to_dict())
        return target


def _snapshot_sha256(snapshot: ValidationEvidenceSnapshot) -> str:
    return hashlib.sha256(
        json.dumps(
            snapshot.to_dict(include_hash=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _relative_log_path(
    stored_path: str | Path | None,
    *,
    control_repo_root: str | Path,
    run_dir: str | Path,
) -> str:
    if not stored_path:
        raise InfrastructureError("validation evidence requires persisted command logs")
    control_root = Path(control_repo_root).resolve()
    artifact_root = Path(run_dir).resolve()
    path = Path(stored_path)
    absolute_log = (
        path.resolve() if path.is_absolute() else (control_root / path).resolve()
    )
    try:
        return absolute_log.relative_to(artifact_root).as_posix()
    except ValueError as exc:
        raise InfrastructureError(
            "validation command log is outside the run directory"
        ) from exc


__all__ = [
    "ValidationCommandEvidence",
    "ValidationEvidenceSnapshot",
    "ValidationEvidenceStatus",
]
