from pathlib import Path

from backend.mapper.file_queue import FileQueueMapper
from codex_loop.models import QueueTaskStatus, TaskQueueSpec
from codex_loop.state import QueueStore


def test_queue_mapper_aggregates_durable_order_and_urls(tmp_path: Path) -> None:
    store = QueueStore(tmp_path)
    spec = TaskQueueSpec.from_inputs(
        "交易管理",
        [
            {"requirement": "新增交易", "acceptance_criteria": ["可以新增"]},
            {"requirement": "交易列表", "acceptance_criteria": ["可以查看"]},
        ],
        queue_id="queue-test",
    )
    spec.base_commit = "a" * 40
    state = store.initialize_queue(spec)
    state.current_task_id = spec.subtasks[0].task_id
    state.subtasks[0].status = QueueTaskStatus.WAITING_REVIEW
    store.save_state(state)
    store.save_report(spec.queue_id, "# Queue report\n")
    source = tmp_path / "source.diff"
    source.write_text("diff --git a/a b/a\n", encoding="utf-8")
    store.save_cumulative_diff(spec.queue_id, source)

    snapshot = FileQueueMapper(tmp_path, store=store).load_snapshot(spec.queue_id)

    assert snapshot is not None
    assert snapshot.current_task_id == spec.subtasks[0].task_id
    assert [task.sequence for task in snapshot.subtasks] == [1, 2]
    assert snapshot.subtasks[0].status == "waiting_review"
    assert snapshot.report_url == "/api/queues/queue-test/report"
    assert snapshot.diff_url == "/api/queues/queue-test/diff"
