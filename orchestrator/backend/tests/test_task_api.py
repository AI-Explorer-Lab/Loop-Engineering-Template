from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.backend.domain.models import TaskSnapshot
from orchestrator.backend.exceptions.business_exception import TaskConflictError
from orchestrator.backend.main import create_app


class FakeTaskService:
    def __init__(self) -> None:
        self.created: tuple[str, list[str]] | None = None
        self.resumed: str | None = None
        self.controlled: tuple[str, str] | None = None
        self.rerun: str | None = None
        self.reviewed_commit_subject: str | None = None
        self.retried: str | None = None

    def start_task(
        self,
        requirement: str,
        acceptance_criteria: list[str],
    ) -> TaskSnapshot:
        self.created = (requirement, acceptance_criteria)
        return _snapshot(status="accepted")

    def get_task(self, task_id: str) -> TaskSnapshot:
        return _snapshot(task_id=task_id, status="running")

    def resume_task(self, task_id: str) -> TaskSnapshot:
        self.resumed = task_id
        return _snapshot(task_id=task_id, status="running")

    def request_control(self, task_id: str, action: str) -> TaskSnapshot:
        self.controlled = (task_id, action)
        status = "pausing" if action == "pause" else "cancelling"
        return _snapshot(task_id=task_id, status=status)

    def rerun_task(self, task_id: str) -> TaskSnapshot:
        self.rerun = task_id
        return _snapshot(task_id="task-rerun", status="accepted")

    def get_report(self, task_id: str) -> str:
        return f"# Report for {task_id}\n"

    def get_diff(self, task_id: str) -> str:
        return f"diff --git a/{task_id} b/{task_id}\n"

    def review_task(
        self,
        task_id: str,
        *,
        decision: str,
        reviewer: str,
        comment: str,
        reviewed_diff_sha256: str,
        commit_subject: str = "",
    ) -> TaskSnapshot:
        self.reviewed_commit_subject = commit_subject
        snapshot = _snapshot(task_id=task_id, status="success")
        values = snapshot.to_dict()
        values.update(
            review_status=decision,
            review={
                "decision": decision,
                "reviewer": reviewer,
                "comment": comment,
                "reviewed_diff_sha256": reviewed_diff_sha256,
                "commit_subject": commit_subject,
            },
        )
        return TaskSnapshot(**values)

    def retry_commit(self, task_id: str) -> TaskSnapshot:
        self.retried = f"commit:{task_id}"
        values = _snapshot(task_id=task_id, status="success").to_dict()
        values["delivery_status"] = "committed"
        return TaskSnapshot(**values)

    def retry_archive(self, task_id: str) -> TaskSnapshot:
        self.retried = f"archive:{task_id}"
        values = _snapshot(task_id=task_id, status="success").to_dict()
        values["delivery_status"] = "archived"
        return TaskSnapshot(**values)


class ConflictingTaskService(FakeTaskService):
    def start_task(
        self,
        requirement: str,
        acceptance_criteria: list[str],
    ) -> TaskSnapshot:
        raise TaskConflictError("Another task is already running")


def _snapshot(
    *,
    task_id: str = "task-1",
    status: str,
) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id,
        requirement="Add filtering",
        acceptance_criteria=["Filtering works"],
        status=status,
        phase="initialized" if status == "running" else None,
        started_at="2026-07-15T08:00:00+08:00",
        updated_at="2026-07-15T08:00:00+08:00",
    )


def _client(service: FakeTaskService) -> TestClient:
    return TestClient(
        create_app(task_service=service, validate_config=False),
        raise_server_exceptions=False,
    )


def test_health_and_create_task() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        health = client.get("/api/health", headers={"X-Request-ID": "request-1"})
        created = client.post(
            "/api/tasks",
            json={
                "requirement": "  Add filtering  ",
                "acceptance_criteria": ["  Filtering works  "],
            },
        )

    assert health.status_code == 200
    assert health.json()["data"]["status"] == "ok"
    assert health.headers["X-Request-ID"] == "request-1"
    assert created.status_code == 202
    assert created.json()["data"]["task_id"] == "task-1"
    assert service.created == ("Add filtering", ["Filtering works"])


def test_invalid_payload_is_422_without_echoing_details() -> None:
    with _client(FakeTaskService()) as client:
        response = client.post(
            "/api/tasks",
            json={"requirement": " ", "acceptance_criteria": []},
        )

    assert response.status_code == 422
    assert response.json()["message"] == "Invalid request payload"


def test_get_resume_and_report() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        fetched = client.get("/api/tasks/task-9")
        resumed = client.post("/api/tasks/task-9/resume")
        report = client.get("/api/tasks/task-9/report")
        diff = client.get("/api/tasks/task-9/diff")

    assert fetched.status_code == 200
    assert fetched.json()["data"]["status"] == "running"
    assert resumed.status_code == 202
    assert service.resumed == "task-9"
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/markdown")
    assert report.text == "# Report for task-9\n"
    assert diff.status_code == 200
    assert diff.headers["content-type"].startswith("text/x-diff")


def test_pause_cancel_and_rerun_endpoints() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        paused = client.post("/api/tasks/task-9/pause")
        cancelled = client.post("/api/tasks/task-9/cancel")
        rerun = client.post("/api/tasks/task-9/rerun")

    assert paused.status_code == 202
    assert paused.json()["data"]["status"] == "pausing"
    assert cancelled.json()["data"]["status"] == "cancelling"
    assert service.controlled == ("task-9", "cancel")
    assert rerun.status_code == 202
    assert rerun.json()["data"]["task_id"] == "task-rerun"
    assert service.rerun == "task-9"


def test_review_endpoint_binds_commit_subject_to_the_recorded_decision() -> None:
    sha = "a" * 64
    service = FakeTaskService()
    with _client(service) as client:
        response = client.post(
            "/api/tasks/task-9/review",
            json={
                "decision": "approved",
                "reviewer": "Local Reviewer",
                "comment": "Checked.",
                "reviewed_diff_sha256": sha,
                "commit_subject": "add filtering",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["review_status"] == "approved"
    assert response.json()["data"]["review"]["reviewed_diff_sha256"] == sha
    assert response.json()["data"]["review"]["commit_subject"] == "add filtering"
    assert service.reviewed_commit_subject == "add filtering"


def test_delivery_and_archive_retry_endpoints_are_separate() -> None:
    service = FakeTaskService()
    with _client(service) as client:
        committed = client.post("/api/tasks/task-9/delivery/retry")
        assert service.retried == "commit:task-9"
        archived = client.post("/api/tasks/task-9/archive/retry")

    assert committed.status_code == 202
    assert committed.json()["data"]["delivery_status"] == "committed"
    assert archived.status_code == 202
    assert archived.json()["data"]["delivery_status"] == "archived"
    assert service.retried == "archive:task-9"


def test_conflict_is_returned_as_structured_409() -> None:
    with _client(ConflictingTaskService()) as client:
        response = client.post(
            "/api/tasks",
            json={
                "requirement": "Add filtering",
                "acceptance_criteria": ["Filtering works"],
            },
        )

    assert response.status_code == 409
    assert response.json()["code"] == "TASK_CONFLICT"
    assert response.json()["success"] is False
