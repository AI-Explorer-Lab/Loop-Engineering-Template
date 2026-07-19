from fastapi.testclient import TestClient

from backend.domain.models import QueueSnapshot, QueueSubtaskSnapshot
from backend.main import create_app


class FakeQueueService:
    def __init__(self) -> None:
        self.created: tuple[str, list[dict[str, object]]] | None = None
        self.resumed: str | None = None
        self.controlled: tuple[str, str] | None = None
        self.rerun: str | None = None
        self.skipped: tuple[str, str] | None = None
        self.reordered: tuple[str, list[str]] | None = None

    def start_queue(self, name: str, subtasks: list[dict[str, object]]) -> QueueSnapshot:
        self.created = (name, subtasks)
        return _queue()

    def get_queue(self, queue_id: str) -> QueueSnapshot:
        return _queue(queue_id=queue_id)

    def resume_queue(self, queue_id: str) -> QueueSnapshot:
        self.resumed = queue_id
        return _queue(queue_id=queue_id, status="infrastructure_error")

    def request_control(self, queue_id: str, action: str) -> QueueSnapshot:
        self.controlled = (queue_id, action)
        return _queue(
            queue_id=queue_id,
            status="pausing" if action == "pause" else "cancelling",
        )

    def rerun_queue(self, queue_id: str) -> QueueSnapshot:
        self.rerun = queue_id
        return _queue(queue_id="queue-rerun")

    def skip_subtask(
        self,
        queue_id: str,
        task_id: str,
        *,
        expected_updated_at: str | None = None,
    ) -> QueueSnapshot:
        self.skipped = (queue_id, task_id)
        return _queue(queue_id=queue_id)

    def reorder_pending(
        self,
        queue_id: str,
        task_ids: list[str],
        *,
        expected_updated_at: str | None = None,
    ) -> QueueSnapshot:
        self.reordered = (queue_id, task_ids)
        return _queue(queue_id=queue_id)

    def get_report(self, queue_id: str) -> str:
        return f"# Queue {queue_id}\n"

    def get_diff(self, queue_id: str) -> str:
        return f"diff --git a/{queue_id} b/{queue_id}\n"


def _queue(
    *, queue_id: str = "queue-1", status: str = "pending"
) -> QueueSnapshot:
    return QueueSnapshot(
        queue_id=queue_id,
        name="交易管理",
        status=status,
        base_ref="HEAD",
        base_commit="a" * 40,
        current_task_id=None,
        cumulative_diff_sha256="",
        last_error_summary="",
        subtasks=[
            QueueSubtaskSnapshot(
                task_id=f"{queue_id}-task-01",
                sequence=1,
                requirement="新增交易",
                acceptance_criteria=["可以新增"],
                status="pending",
                machine_status=None,
                review_status="pending",
                thread_id=None,
                last_error_summary="",
                updated_at="2026-07-17T08:00:00+08:00",
            ),
            QueueSubtaskSnapshot(
                task_id=f"{queue_id}-task-02",
                sequence=2,
                requirement="交易列表",
                acceptance_criteria=["可以查看"],
                status="pending",
                machine_status=None,
                review_status="pending",
                thread_id=None,
                last_error_summary="",
                updated_at="2026-07-17T08:00:00+08:00",
            ),
        ],
        started_at="2026-07-17T08:00:00+08:00",
        updated_at="2026-07-17T08:00:00+08:00",
        finished_at=None,
    )


def test_queue_endpoints_preserve_subtask_request_order() -> None:
    service = FakeQueueService()
    app = create_app(
        task_service=object(),
        queue_service=service,
        validate_config=False,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/queues",
            json={
                "name": " 交易管理 ",
                "subtasks": [
                    {
                        "requirement": " 新增交易 ",
                        "acceptance_criteria": [" 可以新增 "],
                    },
                    {
                        "requirement": "交易列表",
                        "acceptance_criteria": ["可以查看"],
                    },
                ],
            },
        )
        fetched = client.get("/api/queues/queue-9")
        resumed = client.post("/api/queues/queue-9/resume")
        report = client.get("/api/queues/queue-9/report")
        diff = client.get("/api/queues/queue-9/diff")

    assert created.status_code == 202
    assert [item["sequence"] for item in created.json()["data"]["subtasks"]] == [1, 2]
    assert service.created == (
        "交易管理",
        [
            {"requirement": "新增交易", "acceptance_criteria": ["可以新增"]},
            {"requirement": "交易列表", "acceptance_criteria": ["可以查看"]},
        ],
    )
    assert fetched.json()["data"]["queue_id"] == "queue-9"
    assert resumed.status_code == 202
    assert service.resumed == "queue-9"
    assert report.headers["content-type"].startswith("text/markdown")
    assert diff.headers["content-type"].startswith("text/x-diff")


def test_queue_requires_at_least_two_complete_subtasks() -> None:
    app = create_app(
        task_service=object(),
        queue_service=FakeQueueService(),
        validate_config=False,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/queues",
            json={
                "name": "Too short",
                "subtasks": [
                    {"requirement": "One", "acceptance_criteria": ["Works"]}
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["message"] == "Invalid request payload"


def test_queue_control_skip_and_reorder_endpoints() -> None:
    service = FakeQueueService()
    app = create_app(
        task_service=object(),
        queue_service=service,
        validate_config=False,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        paused = client.post("/api/queues/queue-9/pause")
        cancelled = client.post("/api/queues/queue-9/cancel")
        rerun = client.post("/api/queues/queue-9/rerun")
        skipped = client.post("/api/queues/queue-9/subtasks/queue-9-task-02/skip")
        reordered = client.post(
            "/api/queues/queue-9/reorder",
            json={"task_ids": ["queue-9-task-02", "queue-9-task-01"]},
        )

    assert paused.status_code == 202
    assert paused.json()["data"]["status"] == "pausing"
    assert cancelled.json()["data"]["status"] == "cancelling"
    assert service.controlled == ("queue-9", "cancel")
    assert rerun.json()["data"]["queue_id"] == "queue-rerun"
    assert service.rerun == "queue-9"
    assert skipped.status_code == 200
    assert service.skipped == ("queue-9", "queue-9-task-02")
    assert reordered.status_code == 200
    assert service.reordered == (
        "queue-9",
        ["queue-9-task-02", "queue-9-task-01"],
    )
