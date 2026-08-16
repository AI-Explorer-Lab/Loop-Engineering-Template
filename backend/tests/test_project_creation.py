from __future__ import annotations

import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.service.project_registry import ProjectRegistry
from backend.service.project_environment import ProjectEnvironment


def _config(control_root: Path, registry_path: Path) -> dict[str, object]:
    return {
        "environment": {"name": "test", "debug": False},
        "server": {"cors_origins": ["http://localhost"]},
        "agent": {
            "validation_timeout_seconds": 30,
            "max_parallel_projects": 1,
            "project_registry_path": str(registry_path),
            "projects": [
                {
                    "id": "control",
                    "name": "Control",
                    "repo_root": str(control_root),
                    "default": True,
                }
            ],
        },
    }


def _stub_environment(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.service.project_registry.prepare_project_environment",
        lambda *_args, **kwargs: ProjectEnvironment(
            conda_env_name=(
                "loop-project-" + str(kwargs["project_name"]).lower()
                if "python_tests" in kwargs["validation_options"]
                else None
            ),
            frontend_install_command=None,
        ),
    )


def test_new_project_endpoint_creates_git_project_and_persists_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    target = tmp_path / "read-notes"
    registry_path = tmp_path / "projects.json"
    config = _config(control_root, registry_path)
    monkeypatch.setattr(ProjectRegistry, "_validate_knowledge_actor", lambda *_args: None)
    _stub_environment(monkeypatch)

    with TestClient(create_app(config=config, validate_config=False)) as client:
        response = client.post(
            "/api/projects",
            json={
                "name": "ReadingNotes",
                "repo_path": str(target),
                "project_type": "python",
                "validation_options": ["python_tests"],
                "knowledge_actor_id": "zhangsan",
            },
        )
        projects = client.get("/api/projects")

    assert response.status_code == 201
    created = response.json()["data"]
    assert created["project_id"] == "read-notes"
    assert created["repo_root"] == str(target)
    assert created["publish_auto_create_remote"] is True
    assert (target / ".gitignore").is_file()
    project_gitignore = (target / ".gitignore").read_text(encoding="utf-8")
    assert ".codex-runtime/" in project_gitignore
    assert "node_modules/" in project_gitignore
    assert "dist/" in project_gitignore
    harness_config = target / ".harness" / "project.json"
    assert harness_config.is_file()
    assert ".harness/" not in (target / ".gitignore").read_text(encoding="utf-8")
    assert json.loads(harness_config.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "kind": "codex-harness-project",
        "project_id": "read-notes",
        "project_name": "ReadingNotes",
        "state_root": ".codex-orchestrator",
        "secure_runtime_root": ".codex-runtime",
    }
    assert (target / ".git").is_dir()
    assert (target / "tests" / "__init__.py").is_file()
    assert (target / "tests" / "test_smoke.py").is_file()
    assert subprocess.run(
        ["git", "-C", str(target), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == "main"
    tracked = subprocess.run(
        ["git", "-C", str(target), "ls-files", "--cached"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert ".harness/project.json" in tracked
    assert "tests/test_smoke.py" in tracked
    assert "read-notes" in {item["project_id"] for item in projects.json()["data"]}

    restarted = ProjectRegistry(config)
    try:
        assert restarted.get("read-notes").repo_root == target
        assert restarted.get("read-notes").publish_auto_create_remote is True
    finally:
        restarted.close(wait=True)


def test_new_project_persists_one_time_backend_architecture_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    target = tmp_path / "daily-journal"
    registry_path = tmp_path / "projects.json"
    mcp_registry = tmp_path / "mcp-registry.json"
    mcp_registry.write_text(json.dumps({"roots": {}}), encoding="utf-8")
    config = _config(control_root, registry_path)
    config["agent"] = {
        **config["agent"],
        "knowledge": {
            "repo_root": str(tmp_path),
            "mcp_registry": str(mcp_registry),
            "knowledge_writer_actor_id": "orchestrator",
        },
    }
    monkeypatch.setattr(ProjectRegistry, "_validate_knowledge_actor", lambda *_args: None)
    _stub_environment(monkeypatch)

    with TestClient(create_app(config=config, validate_config=False)) as client:
        response = client.post(
            "/api/projects",
            json={
                "name": "DailyJournal",
                "repo_path": str(target),
                "project_type": "python",
                "validation_options": ["python_tests"],
                "knowledge_actor_id": "zhangsan",
                "backend_architecture_enabled": True,
            },
        )

    assert response.status_code == 201
    created = response.json()["data"]
    assert created["backend_architecture_enabled"] is True
    assert created["backend_architecture_knowledge_id"] == "TK-DEC-001"
    assert created["backend_architecture_status"] == "pending"
    assert (target / "backend" / "main.py").is_file()
    assert (target / "backend" / "controller" / "dailyjournal_api.py").is_file()
    assert (target / "backend" / "service" / "dailyjournal_service.py").is_file()
    assert (target / "backend" / "mapper" / "__init__.py").is_file()
    assert (target / "backend" / "utils" / "__init__.py").is_file()
    assert not (target / "backend" / "mapper" / "database_entity.py").exists()
    assert not (target / "backend" / "utils" / "capability.py").exists()
    stored = json.loads(registry_path.read_text(encoding="utf-8"))
    assert stored[0]["backend_architecture_enabled"] is True
    assert stored[0]["backend_architecture_knowledge_id"] == "TK-DEC-001"


def test_delete_project_endpoint_removes_registration_but_keeps_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    target = tmp_path / "accounting"
    registry_path = tmp_path / "projects.json"
    config = _config(control_root, registry_path)
    monkeypatch.setattr(ProjectRegistry, "_validate_knowledge_actor", lambda *_args: None)
    _stub_environment(monkeypatch)

    with TestClient(create_app(config=config, validate_config=False)) as client:
        created = client.post(
            "/api/projects",
            json={
                "name": "accounting",
                "repo_path": str(target),
                "project_type": "python",
                "validation_options": ["python_tests"],
                "knowledge_actor_id": "zhangsan",
            },
        )
        project_id = created.json()["data"]["project_id"]
        deleted = client.delete(f"/api/projects/{project_id}")
        projects = client.get("/api/projects")

    assert deleted.status_code == 200
    assert deleted.json()["data"] == {
        "project_id": project_id,
        "message": "项目配置已删除，项目目录未删除",
    }
    assert target.is_dir()
    assert project_id not in {item["project_id"] for item in projects.json()["data"]}
    assert json.loads(registry_path.read_text(encoding="utf-8")) == []


def test_new_project_rejects_names_that_cannot_bind_to_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    config = _config(control_root, tmp_path / "projects.json")
    monkeypatch.setattr(ProjectRegistry, "_validate_knowledge_actor", lambda *_args: None)
    _stub_environment(monkeypatch)

    with TestClient(create_app(config=config, validate_config=False)) as client:
        response = client.post(
            "/api/projects",
            json={
                "name": "财务 account",
                "repo_path": str(tmp_path / "account"),
                "project_type": "python",
                "validation_options": ["python_tests"],
                "knowledge_actor_id": "zhangsan",
            },
        )

    assert response.status_code == 400
    assert "ASCII 字母、数字和连字符" in str(response.json())


def test_new_project_accepts_hyphenated_name_and_binds_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    target = tmp_path / "accounting-app"
    registry_path = tmp_path / "projects.json"
    config = _config(control_root, registry_path)
    monkeypatch.setattr(ProjectRegistry, "_validate_knowledge_actor", lambda *_args: None)
    _stub_environment(monkeypatch)

    with TestClient(create_app(config=config, validate_config=False)) as client:
        response = client.post(
            "/api/projects",
            json={
                "name": "accounting-app",
                "repo_path": str(target),
                "project_type": "python",
                "validation_options": ["python_tests"],
                "knowledge_actor_id": "zhangsan",
            },
        )

    assert response.status_code == 201
    assert response.json()["data"]["name"] == "accounting-app"
    assert response.json()["data"]["conda_env_name"] == "loop-project-accounting-app"
