"""Run the project's layered validation without executing model-supplied commands.

The runner deliberately builds every command from a small, fixed allow-list.  Test
file paths are discovered from the local workspace and are passed to subprocesses
as individual arguments with ``shell=False``.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Iterable, Mapping, Protocol, Sequence

from .models import (
    PROJECT_TIMEZONE,
    CommandResult,
    InfrastructureError,
    ValidationRound,
)
from .validation_profile import (
    ValidationProfile,
    default_web_validation_profile,
)


DEFAULT_VALIDATION_PROFILE = default_web_validation_profile()
FULL_VALIDATION_COMMANDS = DEFAULT_VALIDATION_PROFILE.full_commands
CONDA_PREFLIGHT_COMMAND = DEFAULT_VALIDATION_PROFILE.preflight[0].command
NPM_PREFLIGHT_COMMAND = DEFAULT_VALIDATION_PROFILE.preflight[1].command


class CommandRunner(Protocol):
    """Injectable process boundary used by the validation runner."""

    def ensure_available(self, executables: Sequence[str]) -> None:
        """Raise :class:`InfrastructureError` for a missing executable."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        stage: str,
        timeout_seconds: float,
    ) -> CommandResult:
        """Run one command and return all output and timing information."""


class SubprocessCommandRunner:
    """Production command runner using argument arrays and ``shell=False``."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        command_prefix: Sequence[str] = (),
    ) -> None:
        self.environment = (
            None
            if environment is None
            else {str(name): str(value) for name, value in environment.items()}
        )
        self.command_prefix = tuple(str(part) for part in command_prefix)

    def ensure_available(self, executables: Sequence[str]) -> None:
        path = None if self.environment is None else self.environment.get("PATH")
        missing = sorted(
            {name for name in executables if shutil.which(name, path=path) is None}
        )
        if missing:
            joined = ", ".join(missing)
            raise InfrastructureError(f"Required executable not found: {joined}")

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        stage: str,
        timeout_seconds: float,
    ) -> CommandResult:
        args = [str(part) for part in command]
        execution_args = [*self.command_prefix, *args]
        started_at = _utc_now()
        started = time.monotonic()

        try:
            completed = subprocess.run(
                execution_args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                env=self.environment,
            )
        except subprocess.TimeoutExpired as exc:
            return _make_command_result(
                command=args,
                cwd=cwd,
                stage=stage,
                started_at=started_at,
                duration_seconds=time.monotonic() - started,
                exit_code=None,
                stdout=_coerce_output(exc.stdout),
                stderr=_coerce_output(exc.stderr),
                timed_out=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            message = f"Could not start {args[0]!r}: {exc}"
            return _make_command_result(
                command=args,
                cwd=cwd,
                stage=stage,
                started_at=started_at,
                duration_seconds=time.monotonic() - started,
                exit_code=None,
                stdout="",
                stderr=message,
                timed_out=False,
                infrastructure_error=message,
            )

        infrastructure_error = _required_runtime_denial(
            completed.stdout, completed.stderr
        )
        return _make_command_result(
            command=args,
            cwd=cwd,
            stage=stage,
            started_at=started_at,
            duration_seconds=time.monotonic() - started,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timed_out=False,
            infrastructure_error=infrastructure_error,
        )


class ValidationRunner:
    """Discover task tests and run targeted then full project validation."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        runner: CommandRunner | None = None,
        environment: Mapping[str, str] | None = None,
        command_prefix: Sequence[str] = (),
        timeout_seconds: float = 900.0,
        baseline_hashes: Mapping[str, str] | None = None,
        protected_test_paths: Sequence[str] | None = None,
        validation_profile: ValidationProfile | Mapping[str, object] | None = None,
    ) -> None:
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            raise InfrastructureError(f"Project root does not exist: {root}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self.project_root = root
        self.validation_profile = (
            validation_profile
            if isinstance(validation_profile, ValidationProfile)
            else ValidationProfile.from_mapping(validation_profile)
        )
        self.runner = runner or SubprocessCommandRunner(
            environment=environment,
            command_prefix=command_prefix,
        )
        self.timeout_seconds = timeout_seconds
        self._baseline = (
            self._snapshot_test_files()
            if baseline_hashes is None
            else {str(path): str(digest) for path, digest in baseline_hashes.items()}
        )
        self._protected_test_paths: set[str] = set()
        self.protect_tests(self._baseline)
        self.protect_tests(protected_test_paths or ())
        self._preflight_complete = False

    @property
    def baseline(self) -> dict[str, str]:
        """Return a copy of the task-start test-file hash baseline."""

        return dict(self._baseline)

    def capture_baseline(self) -> dict[str, str]:
        """Reset the baseline, intended only when starting a new task."""

        self._baseline = self._snapshot_test_files()
        self.protect_tests(self._baseline)
        return self.baseline

    @property
    def protected_test_paths(self) -> tuple[str, ...]:
        """Tests that may not disappear in a later Codex turn."""

        return tuple(sorted(self._protected_test_paths))

    def protect_tests(self, paths: Iterable[str]) -> None:
        """Persistently protect eligible tests observed during the task."""

        for raw_path in paths:
            relative_path = str(raw_path)
            if not self.validation_profile.contains_test(relative_path):
                raise InfrastructureError(
                    f"Invalid protected test path in saved state: {relative_path}"
                )
            self._protected_test_paths.add(relative_path)

    def preflight(self) -> None:
        """Check trusted project paths and runtimes before a round begins."""

        if self._preflight_complete:
            return
        required_paths = tuple(
            self.project_root / relative
            for relative in self.validation_profile.required_paths
        )
        missing = [
            path.relative_to(self.project_root).as_posix()
            for path in required_paths
            if not path.exists()
        ]
        if missing:
            raise InfrastructureError(
                "Required project validation path not found: " + ", ".join(missing)
            )
        self.runner.ensure_available(self.validation_profile.required_executables)

        for probe in self.validation_profile.preflight:
            environment_probe = self.runner.run(
                probe.command,
                cwd=self.project_root,
                stage="preflight",
                timeout_seconds=min(self.timeout_seconds, 60.0),
            )
            if not environment_probe.passed:
                if environment_probe.infrastructure_error:
                    reason = environment_probe.infrastructure_error
                elif environment_probe.timed_out:
                    reason = "probe timed out"
                else:
                    reason = f"probe exited with code {environment_probe.exit_code}"
                raise InfrastructureError(
                    f"{probe.unavailable_message}: {reason}"
                )
        self._preflight_complete = True

    def discover_changed_tests(self) -> tuple[str, ...]:
        """Find tests added or content-modified since the task began.

        Deleted files are not runnable and are therefore omitted.  The comparison
        always uses the task-start baseline, not the preceding Codex turn.
        """

        current = self._snapshot_test_files()
        return self._changed_tests_from_snapshot(current)

    def _changed_tests_from_snapshot(
        self, current: Mapping[str, str]
    ) -> tuple[str, ...]:
        changed = [
            relative_path
            for relative_path, digest in current.items()
            if self._baseline.get(relative_path) != digest
        ]
        return tuple(sorted(changed))

    def discover_deleted_tests(self) -> tuple[str, ...]:
        """Find task-start tests that no longer exist in the workspace."""

        current = self._snapshot_test_files()
        return tuple(sorted(self._protected_test_paths - set(current)))

    def validate(self, round_number: int) -> ValidationRound:
        """Run one complete validation round.

        All discovered targeted groups run even if one fails.  A targeted failure
        skips full validation.  Otherwise every full command runs, even after a
        failure, so one round produces a complete diagnostic set.
        """

        if round_number < 1:
            raise ValueError("round_number must be at least 1")
        self.preflight()
        started_at = _utc_now()

        current_snapshot = self._snapshot_test_files()
        deleted_tests = tuple(
            sorted(self._protected_test_paths - set(current_snapshot))
        )
        self.protect_tests(current_snapshot)
        if deleted_tests:
            message = (
                "Test files present at task start were deleted: "
                + ", ".join(deleted_tests)
            )
            integrity_result = CommandResult(
                command=["internal-test-integrity-check", *deleted_tests],
                cwd=str(self.project_root),
                stage="targeted",
                started_at=started_at,
                duration_seconds=0.0,
                exit_code=1,
                stdout="",
                stderr=message,
            )
            return ValidationRound(
                round_number=round_number,
                passed=False,
                targeted_results=[integrity_result],
                full_results=[],
                stage="targeted",
                started_at=started_at,
                finished_at=_utc_now(),
                failure_summary=message,
            )

        targeted_tests = self._changed_tests_from_snapshot(current_snapshot)
        targeted_results, infrastructure_error = self._run_commands(
            self._targeted_commands(targeted_tests),
            stage="targeted",
        )
        if infrastructure_error:
            return ValidationRound(
                round_number=round_number,
                passed=False,
                targeted_results=targeted_results,
                full_results=[],
                stage="targeted",
                started_at=started_at,
                finished_at=_utc_now(),
                failure_summary=_failure_summary("targeted", targeted_results),
                infrastructure_error=infrastructure_error,
            )
        if _has_failures(targeted_results):
            return ValidationRound(
                round_number=round_number,
                passed=False,
                targeted_results=targeted_results,
                full_results=[],
                stage="targeted",
                started_at=started_at,
                finished_at=_utc_now(),
                failure_summary=_failure_summary("targeted", targeted_results),
            )

        full_results, infrastructure_error = self._run_commands(
            self.validation_profile.full_commands, stage="full"
        )
        if infrastructure_error:
            return ValidationRound(
                round_number=round_number,
                passed=False,
                targeted_results=targeted_results,
                full_results=full_results,
                stage="full",
                started_at=started_at,
                finished_at=_utc_now(),
                failure_summary=_failure_summary("full", full_results),
                infrastructure_error=infrastructure_error,
            )
        if _has_failures(full_results):
            return ValidationRound(
                round_number=round_number,
                passed=False,
                targeted_results=targeted_results,
                full_results=full_results,
                stage="full",
                started_at=started_at,
                finished_at=_utc_now(),
                failure_summary=_failure_summary("full", full_results),
            )

        return ValidationRound(
            round_number=round_number,
            passed=True,
            targeted_results=targeted_results,
            full_results=full_results,
            stage="full",
            started_at=started_at,
            finished_at=_utc_now(),
            failure_summary="",
        )

    def _run_commands(
        self,
        commands: Iterable[Sequence[str]],
        *,
        stage: str,
    ) -> tuple[list[CommandResult], str | None]:
        results: list[CommandResult] = []
        for command in commands:
            try:
                result = self.runner.run(
                    command,
                    cwd=self.project_root,
                    stage=stage,
                    timeout_seconds=self.timeout_seconds,
                )
            except InfrastructureError as exc:
                message = str(exc) or type(exc).__name__
                result = CommandResult(
                    command=[str(part) for part in command],
                    cwd=str(self.project_root),
                    stage=stage,
                    started_at=_utc_now(),
                    duration_seconds=0.0,
                    exit_code=None,
                    stdout="",
                    stderr=message,
                    infrastructure_error=message,
                )
            results.append(result)
            if result.infrastructure_error:
                return results, result.infrastructure_error
        return results, None

    def _targeted_commands(
        self, targeted_tests: Sequence[str]
    ) -> tuple[tuple[str, ...], ...]:
        commands: list[tuple[str, ...]] = []
        for group in self.validation_profile.test_groups:
            group_paths = [path for path in targeted_tests if group.contains(path)]
            if group_paths:
                commands.append(group.command_for(group_paths))
        return tuple(commands)

    def _snapshot_test_files(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in self._eligible_test_files():
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except (FileNotFoundError, PermissionError, OSError) as exc:
                raise InfrastructureError(
                    f"Could not read test file {path}: {exc}"
                ) from exc
            snapshot[path.relative_to(self.project_root).as_posix()] = digest
        return snapshot

    def _eligible_test_files(self) -> tuple[Path, ...]:
        candidates: list[Path] = []
        roots: list[Path] = []
        for group in self.validation_profile.test_groups:
            configured_root = self.project_root / group.root
            if configured_root.is_symlink():
                raise InfrastructureError(
                    f"Configured test root may not be a symbolic link: {group.root}"
                )
            root = configured_root.resolve()
            if not _is_relative_to(root, self.project_root):
                raise InfrastructureError(
                    f"Configured test root escapes the project: {group.root}"
                )
            roots.append(root)
            if not root.is_dir():
                continue
            for directory, directory_names, file_names in os.walk(
                root, followlinks=False
            ):
                directory_names[:] = [
                    name
                    for name in directory_names
                    if name not in group.exclude_directories
                ]
                candidates.extend(
                    Path(directory) / name
                    for name in file_names
                    if any(name.endswith(suffix) for suffix in group.suffixes)
                )

        safe: list[Path] = []
        for candidate in candidates:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if any(_is_relative_to(resolved, root) for root in roots):
                safe.append(resolved)
        return tuple(sorted(set(safe)))


def _make_command_result(**values: object) -> CommandResult:
    """Construct the shared model while tolerating optional reporting fields."""

    values.setdefault("infrastructure_error", None)
    values.setdefault("log_path", None)
    return CommandResult(**values)  # type: ignore[arg-type]


def _has_failures(results: Sequence[CommandResult]) -> bool:
    return any(result.timed_out or result.exit_code != 0 for result in results)


def _failure_summary(stage: str, results: Sequence[CommandResult]) -> str:
    failures: list[str] = []
    for result in results:
        if not (
            result.infrastructure_error
            or result.timed_out
            or result.exit_code != 0
        ):
            continue
        command = " ".join(result.command)
        if result.infrastructure_error:
            reason = f"infrastructure error: {result.infrastructure_error}"
        else:
            reason = (
                "timed out"
                if result.timed_out
                else f"exit code {result.exit_code}"
            )
        output = (result.stderr.strip() or result.stdout.strip())[:2_000]
        detail = f"; output: {output}" if output else ""
        failures.append(f"{command}: {reason}{detail}")
    return f"{stage} validation failed: " + "; ".join(failures)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _utc_now() -> str:
    return datetime.now(PROJECT_TIMEZONE).isoformat()


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _required_runtime_denial(stdout: str, stderr: str) -> str | None:
    normalized = f"{stdout}\n{stderr}".casefold()
    denied = (
        "operation not permitted" in normalized
        or "permission denied" in normalized
        or "kill eperm" in normalized
    )
    required_paths = (
        "node_modules/.cache",
        "node_modules/.tmp",
        "node_modules/.vite",
        "node_modules/.vite-temp",
        "npm-cli.js",
        "sandbox-exec: sandbox_apply",
    )
    if denied and (
        "kill eperm" in normalized
        or any(path in normalized for path in required_paths)
    ):
        return "Validation sandbox denied a required local runtime or cache path"
    return None


__all__ = [
    "CommandRunner",
    "CONDA_PREFLIGHT_COMMAND",
    "FULL_VALIDATION_COMMANDS",
    "NPM_PREFLIGHT_COMMAND",
    "ValidationProfile",
    "InfrastructureError",
    "SubprocessCommandRunner",
    "ValidationRunner",
]
