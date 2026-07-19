from pathlib import Path
import json

import pytest

from orchestrator.backend.mapper.file_run import FileRunMapper
from orchestrator.codex_loop.models import (
    CommandResult,
    ReviewRecord,
    ReviewStatus,
    RunStatus,
    TaskSpec,
    ValidationRound,
)
from orchestrator.codex_loop.report import ReportBuilder
from orchestrator.codex_loop.state import StateStore


def test_mapper_reads_running_and_final_artifacts(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    task = TaskSpec(
        task_id="task-1",
        requirement="Add filtering",
        acceptance_criteria=["Filtering returns matching rows"],
    )
    state = store.initialize_run(task)
    state.thread_id = "thread-1"
    validation_round = ValidationRound(
        round_number=1,
        passed=True,
        stage="full",
        targeted_results=[
            CommandResult(
                command=["pytest", "backend/tests/test_transactions.py"],
                stage="targeted",
                exit_code=0,
                stdout="secret output is intentionally not exposed",
                log_path=".codex-orchestrator/runs/task-1/logs/round-01/01.log",
            )
        ],
    )
    state.add_round(validation_round)
    state.mark_success("M backend/service.py")
    store.save_state(state)
    ReportBuilder().persist(store, task, state)

    mapper = FileRunMapper(tmp_path)
    snapshot = mapper.load_snapshot(task.task_id)

    assert snapshot is not None
    assert snapshot.status == "success"
    assert snapshot.thread_id == "thread-1"
    assert snapshot.report_url == "/api/tasks/task-1/report"
    assert snapshot.rounds[0]["commands"][0]["passed"] is True
    assert "stdout" not in snapshot.rounds[0]["commands"][0]
    assert "# 任务报告" in (mapper.load_report(task.task_id) or "")


def test_mapper_returns_none_for_unknown_safe_task(tmp_path: Path) -> None:
    mapper = FileRunMapper(tmp_path)

    assert mapper.load_task("unknown-task") is None
    assert mapper.load_snapshot("unknown-task") is None
    assert mapper.load_report("unknown-task") is None


def test_mapper_keeps_prior_queue_review_but_exposes_current_pending_status(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    task = TaskSpec(
        task_id="queue-1-task-01",
        queue_id="queue-1",
        sequence=1,
        requirement="Revise the queued change",
        acceptance_criteria=["The revision can be reviewed again"],
    )
    state = store.initialize_run(task)
    state.mark_success()
    store.save_state(state)
    store.save_review_history(
        ReviewRecord(
            task_id=task.task_id,
            decision=ReviewStatus.CHANGES_REQUESTED,
            reviewer="Reviewer",
            comment="Please revise it.",
            machine_status=RunStatus.SUCCESS,
            reviewed_diff_sha256="a" * 64,
        )
    )

    snapshot = FileRunMapper(tmp_path).load_snapshot(task.task_id)

    assert snapshot is not None
    assert snapshot.review_status == "pending"
    assert snapshot.review is not None
    assert snapshot.review["decision"] == "changes_requested"
    assert snapshot.review_history[0]["decision"] == "changes_requested"


def test_mapper_rejects_unsafe_task_id(tmp_path: Path) -> None:
    mapper = FileRunMapper(tmp_path)

    with pytest.raises(ValueError, match="unsafe task id"):
        mapper.load_snapshot("../outside")


def test_legacy_run_is_read_only_and_marked_as_incomplete(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    task = TaskSpec(
        task_id="legacy-task",
        requirement="Historical task",
        acceptance_criteria=["Can still be viewed"],
    )
    state = store.initialize_run(task)
    state.mark_success()
    legacy_task = task.to_dict()
    legacy_task.pop("schema_version")
    legacy_state = state.to_dict()
    legacy_state.pop("schema_version")
    run_dir = store.run_dir(task.task_id)
    (run_dir / "task.json").write_text(json.dumps(legacy_task), encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps(legacy_state), encoding="utf-8")
    (run_dir / "report.md").write_text("# Old report\n", encoding="utf-8")

    snapshot = FileRunMapper(tmp_path).load_snapshot(task.task_id)

    assert snapshot is not None
    assert snapshot.legacy is True
    assert snapshot.schema_version == 0
    assert snapshot.review_status == "unavailable"
    assert snapshot.history_warning
    assert snapshot.workspace == {}
