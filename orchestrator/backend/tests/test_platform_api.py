from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.backend.main import create_app
from orchestrator.backend.service.platform_service import PlatformService
from orchestrator.backend.service.project_registry import ProjectRegistry
from orchestrator.codex_loop.models import TaskSpec
from orchestrator.codex_loop.state import StateStore


def _config(repo_root: Path) -> dict[str, object]:
    return {
        "environment": {"name": "test", "debug": False},
        "server": {"cors_origins": ["http://localhost"]},
        "agent": {
            "repo_root": str(repo_root),
            "validation_timeout_seconds": 30,
            "max_parallel_projects": 2,
            "projects": [
                {
                    "id": "accounting",
                    "name": "Accounting",
                    "repo_root": str(repo_root),
                    "default": True,
                }
            ],
        },
        "notifications": {},
    }


def _persist_completed_task(repo_root: Path) -> TaskSpec:
    store = StateStore(repo_root)
    task = TaskSpec(
        task_id="history-task",
        requirement="Add searchable history",
        acceptance_criteria=["History can be filtered"],
    )
    state = store.initialize_run(task)
    state.thread_id = "thread-history"
    state.mark_success()
    store.save_state(state)
    store.save_report(task.task_id, "# History task\n")
    changes = store.run_dir(task.task_id) / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    (changes / "final.diff").write_text(
        "diff --git a/a.py b/a.py\n+history = True\n",
        encoding="utf-8",
    )
    logs = store.run_dir(task.task_id) / "logs" / "round-01"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "command-01.log").write_text("all tests passed\n", encoding="utf-8")
    store.append_event(task.task_id, "run.completed", {"status": "success"})
    return task


def test_projects_history_events_logs_and_notifications_share_persisted_state(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "accounting"
    repo_root.mkdir()
    task = _persist_completed_task(repo_root)
    app = create_app(config=_config(repo_root), validate_config=False)

    with TestClient(app, raise_server_exceptions=False) as client:
        projects = client.get("/api/projects")
        history = client.get(
            "/api/history",
            params={"project_id": "accounting", "query": "searchable"},
        )
        events = client.get(
            f"/api/history/task/{task.task_id}/events",
            headers={"X-Project-ID": "accounting"},
        )
        logs = client.get(
            f"/api/history/task/{task.task_id}/logs",
            headers={"X-Project-ID": "accounting"},
        )
        log_id = logs.json()["data"][0]["log_id"]
        log = client.get(
            f"/api/history/task/{task.task_id}/logs/{log_id}",
            headers={"X-Project-ID": "accounting"},
        )
        stream = client.get(
            f"/api/history/task/{task.task_id}/stream",
            params={"project_id": "accounting"},
        )
        notifications = client.get(
            "/api/notifications",
            params={"project_id": "accounting", "unread_only": True},
        )
        notification_id = notifications.json()["data"][0]["notification_id"]
        marked = client.post(
            f"/api/notifications/{notification_id}/read",
            headers={"X-Project-ID": "accounting"},
        )
        settings = client.get("/api/notifications/settings")
        updated_settings = client.post(
            "/api/notifications/settings",
            headers={"X-Project-ID": "accounting"},
            json={"in_app": False, "browser": False},
        )
        capabilities = client.get(
            "/api/capabilities", headers={"X-Project-ID": "accounting"}
        )
        metrics = client.get(
            "/api/metrics", headers={"X-Project-ID": "accounting"}
        )

    assert projects.status_code == 200
    assert projects.json()["data"][0]["project_id"] == "accounting"
    assert history.json()["data"]["total"] == 1
    assert history.json()["data"]["items"][0]["identifier"] == task.task_id
    assert events.json()["data"]["items"][0]["type"] == "run.completed"
    assert events.json()["data"]["terminal"] is True
    assert logs.json()["data"][0]["sha256"]
    assert log.text == "all tests passed\n"
    assert "event: end" in stream.text
    assert notifications.json()["data"][0]["category"] == "waiting_review"
    assert marked.json()["data"]["read_at"] is not None
    assert settings.json()["data"] == {
        "in_app": True,
        "browser": True,
        "email_configured": False,
        "webhook_configured": False,
    }
    assert updated_settings.json()["data"] == {
        "in_app": False,
        "browser": False,
        "email_configured": False,
        "webhook_configured": False,
    }
    assert capabilities.json()["data"] == {
        "status": "unavailable",
        "project_id": "accounting",
        "reason": "harness feature is disabled",
    }
    assert metrics.json()["data"]["project_id"] == "accounting"
    assert metrics.json()["data"]["completed_tasks"] == 1


def test_unknown_project_is_rejected_before_artifact_lookup(tmp_path: Path) -> None:
    repo_root = tmp_path / "accounting"
    repo_root.mkdir()
    app = create_app(config=_config(repo_root), validate_config=False)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/tasks/missing-task",
            headers={"X-Project-ID": "outside"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "PROJECT_NOT_FOUND"


def test_notification_delivery_failures_are_recorded_without_changing_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "accounting"
    repo_root.mkdir()
    task = _persist_completed_task(repo_root)
    config = _config(repo_root)
    config["notifications"] = {
        "webhook_url": "https://notify.invalid/hook",
        "smtp_host": "smtp.invalid",
        "smtp_port": 587,
        "smtp_sender": "orchestrator@example.test",
        "smtp_recipient": "reviewer@example.test",
    }

    def fail_delivery(*_args, **_kwargs):
        raise OSError("delivery unavailable")

    monkeypatch.setattr(
        "orchestrator.backend.service.platform_service.urlopen",
        fail_delivery,
    )
    monkeypatch.setattr(
        "orchestrator.backend.service.platform_service.smtplib.SMTP",
        fail_delivery,
    )
    registry = ProjectRegistry(config)
    try:
        notifications = PlatformService(registry, config).notifications(
            project_id="accounting"
        )
        persisted = StateStore(repo_root).load_state(task.task_id)
    finally:
        registry.close(wait=True)

    assert notifications[0]["delivery"]["webhook"].startswith("failed:")
    assert notifications[0]["delivery"]["email"].startswith("failed:")
    assert persisted.status.value == "success"
