from __future__ import annotations

from collections import deque
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.codex_loop.models import CommandResult, InfrastructureError
from orchestrator.codex_loop.validation_runner import (
    CONDA_PREFLIGHT_COMMAND,
    FULL_VALIDATION_COMMANDS,
    NPM_PREFLIGHT_COMMAND,
    SubprocessCommandRunner,
    ValidationProfile,
    ValidationRunner,
)


class FakeCommandRunner:
    def __init__(
        self,
        responses: list[tuple[int | None, bool]] | None = None,
        *,
        unavailable: str | None = None,
        probe_response: tuple[int | None, bool] = (0, False),
        probe_responses: dict[tuple[str, ...], tuple[int | None, bool]]
        | None = None,
    ) -> None:
        self.responses = deque(responses or [])
        self.unavailable = unavailable
        self.probe_response = probe_response
        self.probe_responses = dict(probe_responses or {})
        self.commands: list[list[str]] = []
        self.stages: list[str] = []
        self.preflight_calls: list[tuple[str, ...]] = []
        self.probe_commands: list[list[str]] = []

    def ensure_available(self, executables: tuple[str, ...]) -> None:
        self.preflight_calls.append(tuple(executables))
        if self.unavailable:
            raise InfrastructureError(
                f"Required executable not found: {self.unavailable}"
            )

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        stage: str,
        timeout_seconds: float,
    ) -> CommandResult:
        del timeout_seconds
        args = list(command)
        if stage == "preflight":
            self.probe_commands.append(args)
            exit_code, timed_out = self.probe_responses.get(
                tuple(args), self.probe_response
            )
            return CommandResult(
                command=args,
                cwd=str(cwd),
                stage=stage,
                started_at="2026-07-15T08:00:00+08:00",
                duration_seconds=0.01,
                exit_code=exit_code,
                stdout="",
                stderr="probe failed" if exit_code else "",
                timed_out=timed_out,
            )
        self.commands.append(args)
        self.stages.append(stage)
        exit_code, timed_out = self.responses.popleft() if self.responses else (0, False)
        return CommandResult(
            command=args,
            cwd=str(cwd),
            stage=stage,
            started_at="2026-07-15T08:00:00+08:00",
            duration_seconds=0.01,
            exit_code=exit_code,
            stdout="fake stdout",
            stderr="fake stderr" if exit_code else "",
            timed_out=timed_out,
        )


class ValidationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        (self.root / "backend/tests").mkdir(parents=True)
        (self.root / "frontend/src/components").mkdir(parents=True)
        (self.root / "frontend/package.json").write_text(
            '{"scripts":{"test":"vitest run","build":"vite build"}}\n',
            encoding="utf-8",
        )
        frontend_bin = self.root / "frontend/node_modules/.bin"
        frontend_bin.mkdir(parents=True)
        for tool in ("vitest", "vue-tsc", "vite"):
            (frontend_bin / tool).write_text("", encoding="utf-8")
        self.backend_test = self.root / "backend/tests/test_existing.py"
        self.frontend_test = (
            self.root / "frontend/src/components/ExistingComponent.test.ts"
        )
        self.backend_test.write_text("def test_existing(): pass\n", encoding="utf-8")
        self.frontend_test.write_text("test('existing', () => {})\n", encoding="utf-8")

    def test_discovers_added_and_modified_tests_and_runs_layered_commands(self) -> None:
        fake = FakeCommandRunner()
        validator = ValidationRunner(self.root, runner=fake)

        self.backend_test.write_text("def test_existing(): assert True\n", encoding="utf-8")
        new_frontend = self.root / "frontend/src/components/NewComponent.test.ts"
        new_frontend.write_text("test('new', () => {})\n", encoding="utf-8")

        self.assertEqual(
            validator.discover_changed_tests(),
            (
                "backend/tests/test_existing.py",
                "frontend/src/components/NewComponent.test.ts",
            ),
        )
        result = validator.validate(1)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.targeted_results), 2)
        self.assertEqual(len(result.full_results), 3)
        self.assertEqual(
            fake.commands[0],
            [
                "conda",
                "run",
                "-n",
                "loop-engineering",
                "pytest",
                "-q",
                "backend/tests/test_existing.py",
            ],
        )
        self.assertEqual(
            fake.commands[1],
            [
                "npm",
                "--prefix",
                "frontend",
                "test",
                "--",
                "src/components/NewComponent.test.ts",
            ],
        )
        self.assertEqual(
            [tuple(command) for command in fake.commands[2:]],
            list(FULL_VALIDATION_COMMANDS),
        )
        self.assertEqual(fake.stages, ["targeted", "targeted", "full", "full", "full"])

    def test_no_changed_tests_falls_back_to_all_full_commands(self) -> None:
        fake = FakeCommandRunner()
        validator = ValidationRunner(self.root, runner=fake)

        result = validator.validate(1)

        self.assertTrue(result.passed)
        self.assertEqual(result.targeted_results, [])
        self.assertEqual(len(result.full_results), 3)
        self.assertEqual(
            [tuple(command) for command in fake.commands],
            list(FULL_VALIDATION_COMMANDS),
        )

    def test_targeted_failure_runs_all_targeted_groups_and_skips_full(self) -> None:
        fake = FakeCommandRunner([(1, False), (0, False)])
        validator = ValidationRunner(self.root, runner=fake)
        self.backend_test.write_text("def test_existing(): assert False\n", encoding="utf-8")
        self.frontend_test.write_text("test('changed', () => {})\n", encoding="utf-8")

        result = validator.validate(2)

        self.assertFalse(result.passed)
        self.assertEqual(result.round_number, 2)
        self.assertEqual(result.stage, "targeted")
        self.assertEqual(len(result.targeted_results), 2)
        self.assertEqual(result.full_results, [])
        self.assertEqual(len(fake.commands), 2)
        self.assertIn("exit code 1", result.failure_summary)

    def test_full_stage_always_runs_all_three_commands_after_failures(self) -> None:
        fake = FakeCommandRunner([(1, False), (2, False), (0, False)])
        validator = ValidationRunner(self.root, runner=fake)

        result = validator.validate(3)

        self.assertFalse(result.passed)
        self.assertEqual(result.stage, "full")
        self.assertEqual(len(result.full_results), 3)
        self.assertEqual([item.exit_code for item in result.full_results], [1, 2, 0])
        self.assertEqual(len(fake.commands), 3)
        self.assertIn("exit code 1", result.failure_summary)
        self.assertIn("exit code 2", result.failure_summary)

    def test_timeout_is_an_ordinary_validation_failure(self) -> None:
        fake = FakeCommandRunner([(None, True)])
        validator = ValidationRunner(self.root, runner=fake)
        self.backend_test.write_text("def test_existing(): assert False\n", encoding="utf-8")

        result = validator.validate(1)

        self.assertFalse(result.passed)
        self.assertEqual(result.stage, "targeted")
        self.assertTrue(result.targeted_results[0].timed_out)
        self.assertIsNone(result.targeted_results[0].exit_code)
        self.assertIn("timed out", result.failure_summary)

    def test_missing_executable_is_infrastructure_error_not_round_result(self) -> None:
        fake = FakeCommandRunner(unavailable="npm")
        validator = ValidationRunner(self.root, runner=fake)

        with self.assertRaisesRegex(InfrastructureError, "npm"):
            validator.validate(1)

        self.assertEqual(fake.commands, [])
        self.assertEqual(fake.preflight_calls, [("conda", "npm")])

    def test_missing_project_validation_path_is_infrastructure_error(self) -> None:
        (self.root / "frontend/package.json").unlink()
        fake = FakeCommandRunner()
        validator = ValidationRunner(self.root, runner=fake)

        with self.assertRaisesRegex(InfrastructureError, "frontend/package.json"):
            validator.validate(1)

        self.assertEqual(fake.preflight_calls, [])

    def test_missing_loop_engineering_environment_or_pytest_is_infrastructure_error(
        self,
    ) -> None:
        fake = FakeCommandRunner(probe_response=(1, False))
        validator = ValidationRunner(self.root, runner=fake)

        with self.assertRaisesRegex(
            InfrastructureError, "loop-engineering.*pytest"
        ):
            validator.validate(1)

        self.assertEqual(fake.commands, [])
        self.assertEqual(fake.probe_commands, [list(CONDA_PREFLIGHT_COMMAND)])

    def test_missing_node_runtime_is_infrastructure_error_before_codex_turn(
        self,
    ) -> None:
        fake = FakeCommandRunner(
            probe_responses={NPM_PREFLIGHT_COMMAND: (1, False)}
        )
        validator = ValidationRunner(self.root, runner=fake)

        with self.assertRaisesRegex(InfrastructureError, "Node/npm"):
            validator.validate(1)

        self.assertEqual(fake.commands, [])
        self.assertEqual(
            fake.probe_commands,
            [list(CONDA_PREFLIGHT_COMMAND), list(NPM_PREFLIGHT_COMMAND)],
        )

    def test_missing_frontend_tool_is_infrastructure_error(self) -> None:
        (self.root / "frontend/node_modules/.bin/vitest").unlink()
        fake = FakeCommandRunner()
        validator = ValidationRunner(self.root, runner=fake)

        with self.assertRaisesRegex(InfrastructureError, "vitest"):
            validator.validate(1)

        self.assertEqual(fake.probe_commands, [])

    def test_custom_profile_uses_project_specific_tests_and_commands(self) -> None:
        custom_root = self.root / "service/specs"
        custom_root.mkdir(parents=True)
        custom_test = custom_root / "feature.spec.py"
        custom_test.write_text("def test_feature(): pass\n", encoding="utf-8")
        profile = ValidationProfile.from_mapping(
            {
                "required_paths": ["service/specs"],
                "dependency_paths": ["web/node_modules"],
                "preflight": [
                    {
                        "command": ["python", "-c", "import pytest"],
                        "unavailable_message": "project Python is unavailable",
                    }
                ],
                "test_groups": [
                    {
                        "name": "service-python",
                        "root": "service/specs",
                        "path_base": ".",
                        "suffixes": [".spec.py"],
                        "command": ["python", "-m", "pytest", "{tests}"],
                    }
                ],
                "full_commands": [
                    ["python", "-m", "pytest", "service/specs"]
                ],
            }
        )
        fake = FakeCommandRunner()
        validator = ValidationRunner(
            self.root,
            runner=fake,
            validation_profile=profile,
        )
        custom_test.write_text("def test_feature(): assert True\n", encoding="utf-8")

        result = validator.validate(1)

        self.assertTrue(result.passed)
        self.assertEqual(profile.dependency_paths, (Path("web/node_modules"),))
        self.assertEqual(
            fake.commands,
            [
                ["python", "-m", "pytest", "service/specs/feature.spec.py"],
                ["python", "-m", "pytest", "service/specs"],
            ],
        )
        self.assertEqual(fake.preflight_calls, [("python",)])

    def test_custom_profile_rejects_unsafe_or_shell_style_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            ValidationProfile.from_mapping(
                {
                    "required_paths": ["../outside"],
                    "full_commands": [["python", "-m", "pytest"]],
                }
            )
        with self.assertRaisesRegex(ValueError, "must be a list"):
            ValidationProfile.from_mapping(
                {"full_commands": ["python -m pytest"]}
            )

    def test_custom_profile_rejects_a_symlinked_test_root(self) -> None:
        real_root = self.root / "real-tests"
        real_root.mkdir()
        (real_root / "test_safe.py").write_text(
            "def test_safe(): pass\n", encoding="utf-8"
        )
        (self.root / "linked-tests").symlink_to(
            real_root, target_is_directory=True
        )
        profile = ValidationProfile.from_mapping(
            {
                "required_paths": ["linked-tests"],
                "test_groups": [
                    {
                        "name": "linked",
                        "root": "linked-tests",
                        "path_base": ".",
                        "suffixes": [".py"],
                        "command": ["python", "-m", "pytest", "{tests}"],
                    }
                ],
                "full_commands": [["python", "-m", "pytest"]],
            }
        )

        with self.assertRaisesRegex(InfrastructureError, "symbolic link"):
            ValidationRunner(
                self.root,
                runner=FakeCommandRunner(),
                validation_profile=profile,
            )

    def test_supplied_baseline_survives_process_resume(self) -> None:
        initial = ValidationRunner(self.root, runner=FakeCommandRunner())
        saved_baseline = initial.baseline
        self.backend_test.write_text("def test_existing(): assert True\n", encoding="utf-8")

        resumed = ValidationRunner(
            self.root,
            runner=FakeCommandRunner(),
            baseline_hashes=saved_baseline,
        )

        self.assertEqual(
            resumed.discover_changed_tests(),
            ("backend/tests/test_existing.py",),
        )

    def test_generated_frontend_directories_are_not_scanned(self) -> None:
        generated = self.root / "frontend/node_modules/pkg/generated.test.ts"
        generated.parent.mkdir(parents=True)
        generated.write_text("test('generated', () => {})\n", encoding="utf-8")
        validator = ValidationRunner(self.root, runner=FakeCommandRunner())

        generated.write_text("test('changed generated', () => {})\n", encoding="utf-8")

        self.assertEqual(validator.discover_changed_tests(), ())

    def test_deleted_task_start_test_fails_integrity_check_and_skips_commands(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        validator = ValidationRunner(self.root, runner=fake)
        self.backend_test.unlink()

        result = validator.validate(1)

        self.assertFalse(result.passed)
        self.assertEqual(result.stage, "targeted")
        self.assertEqual(result.full_results, [])
        self.assertEqual(len(result.targeted_results), 1)
        self.assertEqual(
            result.targeted_results[0].command[0],
            "internal-test-integrity-check",
        )
        self.assertIn("backend/tests/test_existing.py", result.failure_summary)
        self.assertEqual(fake.commands, [])

    def test_test_added_in_one_round_remains_protected_in_later_round(self) -> None:
        fake = FakeCommandRunner()
        validator = ValidationRunner(self.root, runner=fake)
        new_test = self.root / "backend/tests/test_added_by_codex.py"
        new_test.write_text("def test_added(): assert True\n", encoding="utf-8")

        first = validator.validate(1)

        self.assertTrue(first.passed)
        self.assertIn(
            "backend/tests/test_added_by_codex.py",
            validator.protected_test_paths,
        )
        commands_after_first = len(fake.commands)
        new_test.unlink()

        second = validator.validate(2)

        self.assertFalse(second.passed)
        self.assertIn("test_added_by_codex.py", second.failure_summary)
        self.assertEqual(len(fake.commands), commands_after_first)

    def test_saved_protected_tests_survive_validator_reconstruction(self) -> None:
        initial = ValidationRunner(self.root, runner=FakeCommandRunner())
        new_test = self.root / "backend/tests/test_added_by_codex.py"
        new_test.write_text("def test_added(): assert True\n", encoding="utf-8")
        initial.validate(1)
        protected = initial.protected_test_paths
        new_test.unlink()

        resumed = ValidationRunner(
            self.root,
            runner=FakeCommandRunner(),
            baseline_hashes=initial.baseline,
            protected_test_paths=protected,
        )
        result = resumed.validate(2)

        self.assertFalse(result.passed)
        self.assertIn("test_added_by_codex.py", result.failure_summary)

    def test_infrastructure_error_keeps_prior_command_results_in_round(self) -> None:
        class PartialInfrastructureRunner(FakeCommandRunner):
            def run(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
                if len(self.commands) == 1:
                    raise InfrastructureError("second command could not start")
                return super().run(command, **kwargs)  # type: ignore[arg-type]

        fake = PartialInfrastructureRunner([(0, False)])
        validator = ValidationRunner(self.root, runner=fake)

        result = validator.validate(1)

        self.assertFalse(result.passed)
        self.assertEqual(result.stage, "full")
        self.assertEqual(len(result.full_results), 2)
        self.assertTrue(result.full_results[0].passed)
        self.assertEqual(
            result.full_results[1].infrastructure_error,
            "second command could not start",
        )
        self.assertEqual(result.infrastructure_error, "second command could not start")
        self.assertIn("infrastructure error", result.failure_summary)

    def test_subprocess_start_error_is_returned_for_round_persistence(self) -> None:
        runner = SubprocessCommandRunner()
        with patch(
            "orchestrator.codex_loop.validation_runner.subprocess.run",
            side_effect=OSError("cannot spawn"),
        ):
            result = runner.run(
                ("conda", "--version"),
                cwd=self.root,
                stage="full",
                timeout_seconds=1,
            )

        self.assertIsNone(result.exit_code)
        self.assertIn("cannot spawn", result.infrastructure_error or "")
        self.assertIn("cannot spawn", result.stderr)

    def test_required_node_cache_denial_is_an_infrastructure_error(self) -> None:
        runner = SubprocessCommandRunner()
        completed = subprocess.CompletedProcess(
            args=["npm", "--prefix", "frontend", "test"],
            returncode=1,
            stdout="",
            stderr=(
                "Error: EPERM: operation not permitted, open "
                "'frontend/node_modules/.vite-temp/config.mjs'"
            ),
        )
        with patch(
            "orchestrator.codex_loop.validation_runner.subprocess.run",
            return_value=completed,
        ):
            result = runner.run(
                ("npm", "--prefix", "frontend", "test"),
                cwd=self.root,
                stage="full",
                timeout_seconds=1,
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("required local runtime", result.infrastructure_error or "")

    def test_worker_signal_denial_is_an_infrastructure_error(self) -> None:
        runner = SubprocessCommandRunner()
        completed = subprocess.CompletedProcess(
            args=["npm", "--prefix", "frontend", "test"],
            returncode=1,
            stdout="",
            stderr="Error: kill EPERM",
        )
        with patch(
            "orchestrator.codex_loop.validation_runner.subprocess.run",
            return_value=completed,
        ):
            result = runner.run(
                ("npm", "--prefix", "frontend", "test"),
                cwd=self.root,
                stage="full",
                timeout_seconds=1,
            )

        self.assertIn("required local runtime", result.infrastructure_error or "")

    def test_subprocess_timeout_is_returned_with_captured_output(self) -> None:
        runner = SubprocessCommandRunner()
        timeout = subprocess.TimeoutExpired(
            cmd=["conda", "--version"],
            timeout=1,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )
        with patch(
            "orchestrator.codex_loop.validation_runner.subprocess.run",
            side_effect=timeout,
        ):
            result = runner.run(
                ("conda", "--version"),
                cwd=self.root,
                stage="full",
                timeout_seconds=1,
            )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.stdout, "partial stdout")
        self.assertEqual(result.stderr, "partial stderr")
        self.assertGreaterEqual(result.duration_seconds, 0)


if __name__ == "__main__":
    unittest.main()
