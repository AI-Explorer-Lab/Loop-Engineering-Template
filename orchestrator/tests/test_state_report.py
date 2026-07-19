from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.codex_loop.models import (
    CommandResult,
    RunResult,
    RunState,
    RunStatus,
    TaskSpec,
    ValidationRound,
    generate_task_id,
    utc_now_iso,
)
from orchestrator.codex_loop.report import (
    PromptRenderer,
    ReportBuilder,
    render_repair_prompt,
)
from orchestrator.codex_loop.state import (
    ActiveRunError,
    StateStore,
    redact_sensitive_text,
)


class StateAndReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(self.temp_dir.name)
        self.store = StateStore(self.repo_root)

    def _task(self, task_id: str = "task-001") -> TaskSpec:
        return TaskSpec(
            task_id=task_id,
            requirement="新增最低金额筛选",
            acceptance_criteria=["最低金额筛选返回正确记录"],
        )

    def _command(
        self,
        *,
        exit_code: int | None = 0,
        stdout: str = "passed",
        stderr: str = "",
        log_path: str | None = None,
    ) -> CommandResult:
        return CommandResult(
            command=["pytest", "-q", "backend/tests/test_transactions.py"],
            cwd=str(self.repo_root),
            stage="targeted",
            started_at="2026-07-15T08:00:00+08:00",
            duration_seconds=0.25,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            log_path=log_path,
        )

    def test_task_spec_generates_safe_id_validates_and_round_trips_json(self) -> None:
        generated = TaskSpec.from_dict(
            {
                "requirement": "实现查询",
                "acceptance_criteria": ["查询结果正确"],
            }
        )
        self.assertRegex(
            generated.task_id,
            r"^\d{8}-\d{6}-[0-9a-f]{8}$",
        )
        self.assertNotIn("/", generated.task_id)
        self.assertEqual(TaskSpec.from_dict(generated.to_dict()), generated)

        task_file = self.repo_root / "input.json"
        task_file.write_text(
            json.dumps(generated.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
        self.assertEqual(TaskSpec.from_file(task_file), generated)

        invalid_inputs = [
            {"requirement": "", "acceptance_criteria": ["ok"]},
            {"requirement": "work", "acceptance_criteria": []},
            {"requirement": "work", "acceptance_criteria": [""]},
            {
                "task_id": "../escape",
                "requirement": "work",
                "acceptance_criteria": ["ok"],
            },
        ]
        for data in invalid_inputs:
            with self.subTest(data=data), self.assertRaises(ValueError):
                TaskSpec.from_dict(data)

    def test_task_id_uses_utc_plus_8_time(self) -> None:
        fixed_time = datetime(2026, 7, 15, 21, 56, 22)

        with patch("orchestrator.codex_loop.models.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = fixed_time
            task_id = generate_task_id()

        task_timezone = mocked_datetime.now.call_args.args[0]
        self.assertEqual(task_timezone.utcoffset(None), timedelta(hours=8))
        self.assertRegex(task_id, r"^20260715-215622-[0-9a-f]{8}$")

    def test_persisted_timestamps_use_utc_plus_8_time(self) -> None:
        timestamp = datetime.fromisoformat(utc_now_iso())

        self.assertEqual(timestamp.utcoffset(), timedelta(hours=8))

    def test_command_result_normalizes_paths_before_json_persistence(self) -> None:
        log_path = self.repo_root / "logs" / "command.log"
        command = CommandResult(
            command=["pytest", "-q"],
            cwd=self.repo_root,
            log_path=log_path,
        )

        self.assertEqual(command.cwd, str(self.repo_root))
        self.assertEqual(command.log_path, str(log_path))
        self.assertEqual(
            json.loads(json.dumps(command.to_dict())),
            command.to_dict(),
        )

    def test_redaction_does_not_treat_standard_pwd_as_a_password(self) -> None:
        workspace_path = "/workspace/accounting"

        self.assertEqual(
            redact_sensitive_text(workspace_path, environ={"PWD": workspace_path}),
            workspace_path,
        )
        self.assertEqual(
            redact_sensitive_text("database-secret", environ={"DB_PWD": "database-secret"}),
            "[REDACTED]",
        )

    def test_state_store_atomically_round_trips_all_artifacts(self) -> None:
        task = self._task()
        state = self.store.initialize_run(
            task,
            baseline_git_status="clean",
            baseline_test_hashes={"backend/tests/test_x.py": "abc123"},
        )
        self.assertEqual(
            state.protected_test_paths, ["backend/tests/test_x.py"]
        )
        state.thread_id = "thread-123"
        state.turn_count = 1
        command = self._command()
        self.store.write_command_log(task.task_id, 1, 1, command)
        validation_round = ValidationRound(
            round_number=1,
            targeted_results=[command],
            passed=True,
            stage="full",
            started_at="2026-07-15T08:00:00+08:00",
            finished_at="2026-07-15T08:00:01+08:00",
        )
        state.add_round(validation_round)
        state.mark_success("M backend/controller/transaction_api.py")

        self.store.save_round(task.task_id, validation_round)
        self.store.save_state(state)
        result = RunResult.from_run(task, state)
        self.store.save_result(result)
        self.store.save_report(task.task_id, "# report\n")

        self.assertEqual(self.store.load_task(task.task_id), task)
        self.assertEqual(
            self.store.load_state(task.task_id).to_dict(include_output=False),
            state.to_dict(include_output=False),
        )
        self.assertEqual(
            self.store.load_round(task.task_id, 1).to_dict(include_output=False),
            validation_round.to_dict(include_output=False),
        )
        self.assertEqual(
            self.store.load_result(task.task_id).to_dict(), result.to_dict()
        )
        run_dir = self.store.run_dir(task.task_id)
        for artifact in ("task.json", "state.json", "result.json", "report.md"):
            self.assertTrue((run_dir / artifact).is_file())
        state_text = (run_dir / "state.json").read_text(encoding="utf-8")
        self.assertNotIn('"stdout"', state_text)
        self.assertNotIn('"stderr"', state_text)
        self.assertTrue(command.log_sha256)
        self.assertEqual(list(run_dir.rglob("*.tmp")), [])

    def test_active_lock_rejects_a_second_task_until_released(self) -> None:
        first_lock = self.store.acquire_active_lock("task-first")
        self.addCleanup(self.store.release_active_lock, first_lock)

        with self.assertRaisesRegex(ActiveRunError, "task-first"):
            self.store.acquire_active_lock("task-second")

        self.store.release_active_lock(first_lock)
        second_lock = self.store.acquire_active_lock("task-second")
        self.store.release_active_lock(second_lock)
        self.assertFalse(self.store.active_lock_path.exists())

    def test_dead_process_stale_lock_is_replaced(self) -> None:
        self.store.root.mkdir(parents=True)
        self.store.active_lock_path.write_text(
            json.dumps(
                {
                    "task_id": "dead-task",
                    "pid": 999_999_999,
                    "token": "dead-token",
                    "hostname": socket.gethostname(),
                    "acquired_at": "2026-07-14T08:00:00+08:00",
                }
            ),
            encoding="utf-8",
        )

        with patch(
            "orchestrator.codex_loop.state.os.kill",
            side_effect=ProcessLookupError,
        ):
            replacement = self.store.acquire_active_lock("replacement-task")

        metadata = json.loads(self.store.active_lock_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["task_id"], "replacement-task")
        self.assertNotEqual(metadata["token"], "dead-token")
        self.store.release_active_lock(replacement)

    def test_secrets_are_filtered_everywhere_and_full_log_is_not_truncated(
        self,
    ) -> None:
        environment_secret = "environment-value-987654"
        api_key = "sk-abcdefghijklmnopqrstuvwxyz"
        bearer_value = "bearer-value-123456"
        token_value = "token-value-123456"
        password_value = "password-value-123456"
        explicit_secret = "secret-value-123456"
        github_token = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        jwt_token = "eyJabcde.abcdefghij.klmnopqrst"
        basic_value = "YWxhZGRpbjpvcGVuc2VzYW1l"
        database_password = "database-password-123456"
        long_output = (
            "A" * 12_000
            + f"\n{api_key}\nBearer {bearer_value}\ntoken={token_value}\n"
            + f"password={password_value}\nsecret={explicit_secret}\n"
            + f"{github_token}\n{jwt_token}\nBasic {basic_value}\n"
            + f"postgresql://account:{database_password}@localhost/db\n"
            + environment_secret
            + "\nEND-OF-LONG-LOG"
        )
        raw_secrets = [
            environment_secret,
            api_key,
            bearer_value,
            token_value,
            password_value,
            explicit_secret,
            github_token,
            jwt_token,
            basic_value,
            database_password,
        ]

        with patch.dict(os.environ, {"SERVICE_SECRET": environment_secret}):
            task = self._task("secret-task")
            state = self.store.initialize_run(
                task,
                baseline_git_status=f"Bearer {bearer_value}",
            )
            state.thread_id = "thread-secret"
            state.turn_count = 3
            command = self._command(exit_code=1, stdout=long_output)
            self.store.write_command_log(task.task_id, 1, 1, command)
            validation_round = ValidationRound(
                round_number=1,
                targeted_results=[command],
                passed=False,
                stage="targeted",
                failure_summary=(
                    f"token={token_value}; password={password_value}; "
                    f"secret={explicit_secret}; {environment_secret}"
                ),
            )
            state.add_round(validation_round)
            state.failure_count = 3
            state.mark_manual_review(f"API key {api_key}")
            self.store.save_round(task.task_id, validation_round)
            self.store.save_state(state)
            result_path, report_path = ReportBuilder().persist(
                self.store, task, state
            )

        log_path = self.repo_root / str(command.log_path)
        artifacts = [
            log_path,
            self.store.run_dir(task.task_id) / "state.json",
            result_path,
            report_path,
        ]
        for artifact in artifacts:
            with self.subTest(artifact=artifact):
                content = artifact.read_text(encoding="utf-8")
                for secret in raw_secrets:
                    self.assertNotIn(secret, content)
                if artifact != report_path:
                    self.assertIn("[REDACTED]", content)

        complete_log = log_path.read_text(encoding="utf-8")
        self.assertIn("A" * 10_000, complete_log)
        self.assertIn("END-OF-LONG-LOG", complete_log)
        self.assertNotIn("truncated", complete_log.lower())

    def test_repair_prompt_redacts_and_truncates_failure_output(self) -> None:
        environment_secret = "environment-repair-secret"
        task = self._task("repair-task")
        state = RunState(task_id=task.task_id, repo_root=str(self.repo_root))
        command = self._command(
            exit_code=1,
            stderr=(
                "Bearer bearer-repair-secret\n"
                "token=token-repair-secret\n"
                + "X" * 20_000
                + environment_secret
            ),
        )
        validation_round = ValidationRound(
            round_number=1,
            targeted_results=[command],
            passed=False,
            stage="targeted",
            failure_summary="password=password-repair-secret",
        )
        state.add_round(validation_round)

        with patch.dict(os.environ, {"REPAIR_SECRET": environment_secret}):
            prompt = render_repair_prompt(
                task,
                state,
                validation_round,
                max_chars=500,
            )

        for secret in (
            "bearer-repair-secret",
            "token-repair-secret",
            "password-repair-secret",
            environment_secret,
        ):
            self.assertNotIn(secret, prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertIn("[truncated", prompt)
        self.assertNotIn("X" * 1_000, prompt)
        self.assertLess(len(prompt), 3_000)

    def test_all_codex_prompts_use_git_ls_files_for_project_inventory(self) -> None:
        task = self._task("prompt-file-listing")
        state = RunState(task_id=task.task_id, repo_root=str(self.repo_root))
        validation_round = ValidationRound(
            round_number=1,
            passed=False,
            failure_summary="targeted validation failed",
        )
        renderer = PromptRenderer()

        prompts = (
            renderer.initial_prompt(task, state),
            renderer.repair_prompt(task, state, validation_round),
            renderer.review_repair_prompt(task, state, "请修复审查问题"),
        )

        expected = "git ls-files --cached --others --exclude-standard"
        for prompt in prompts:
            with self.subTest(prompt=prompt[:40]):
                self.assertIn(expected, prompt)
                self.assertIn("不要执行 `rg`", prompt)

    def test_all_final_status_reports_include_required_audit_fields(self) -> None:
        cases = (
            (RunStatus.SUCCESS, None),
            (RunStatus.MANUAL_REVIEW, None),
            (RunStatus.INFRASTRUCTURE_ERROR, "npm missing"),
        )
        for status, infrastructure_error in cases:
            with self.subTest(status=status.value):
                task = self._task(f"report-{status.value}")
                state = RunState(
                    task_id=task.task_id,
                    repo_root=str(self.repo_root),
                    thread_id="thread-audit",
                    turn_count=3,
                    baseline_git_status=" M existing.py",
                )
                command = self._command(
                    exit_code=0 if status is RunStatus.SUCCESS else 1,
                    stderr="assertion failed" if status is not RunStatus.SUCCESS else "",
                    log_path=(
                        f".codex-orchestrator/runs/{task.task_id}/logs/"
                        "round-01/01-targeted.log"
                    ),
                )
                validation_round = ValidationRound(
                    round_number=1,
                    targeted_results=[command],
                    passed=status is RunStatus.SUCCESS,
                    stage="targeted",
                    failure_summary=(
                        "" if status is RunStatus.SUCCESS else "assertion failed"
                    ),
                )
                state.rounds = [validation_round]
                if status is RunStatus.SUCCESS:
                    state.mark_success("M final.py")
                elif status is RunStatus.MANUAL_REVIEW:
                    state.failure_count = 3
                    state.mark_manual_review("M final.py")
                else:
                    state.mark_infrastructure_error(
                        infrastructure_error or "infrastructure error",
                        "M final.py",
                    )

                result, report = ReportBuilder().build(task, state)

                self.assertEqual(result.status, status)
                for expected in (
                    "# 任务报告",
                    task.requirement,
                    task.acceptance_criteria[0],
                    "thread-audit",
                    "第 1 轮",
                    str(command.log_path),
                    "## 工作区隔离",
                    "## 权限",
                    "## 人工审查",
                    "pending",
                ):
                    self.assertIn(expected, report)


if __name__ == "__main__":
    unittest.main()
