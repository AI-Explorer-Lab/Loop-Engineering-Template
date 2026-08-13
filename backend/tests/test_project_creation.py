from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.service.project_registry import ProjectRegistry


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


def test_new_project_endpoint_creates_git_project_and_persists_registration(
    tmp_path: Path,
) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    target = tmp_path / "read-notes"
    registry_path = tmp_path / "projects.json"
    config = _config(control_root, registry_path)

    with TestClient(create_app(config=config, validate_config=False)) as client:
        response = client.post(
            "/api/projects",
            json={"name": "Reading Notes", "repo_path": str(target)},
        )
        projects = client.get("/api/projects")

    assert response.status_code == 201
    created = response.json()["data"]
    assert created["project_id"] == "read-notes"
    assert created["repo_root"] == str(target)
    assert (target / ".gitignore").is_file()
    assert ".codex-runtime/" in (target / ".gitignore").read_text(encoding="utf-8")
    assert (target / ".git").is_dir()
    assert "read-notes" in {item["project_id"] for item in projects.json()["data"]}

    restarted = ProjectRegistry(config)
    try:
        assert restarted.get("read-notes").repo_root == target
    finally:
        restarted.close(wait=True)
