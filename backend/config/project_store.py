"""Persistent project registration for projects created from the web UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_loop.state import _atomic_write_text, redact_sensitive_data


DEFAULT_PROJECT_STORE = Path(__file__).resolve().parents[2] / ".codex-orchestrator" / "projects.json"


def project_store_path(config: Any) -> Path:
    agent = config.get("agent", {}) or {}
    configured = agent.get("project_registry_path")
    if configured in {None, ""}:
        return DEFAULT_PROJECT_STORE
    path = Path(str(configured)).expanduser()
    if not path.is_absolute():
        path = DEFAULT_PROJECT_STORE.parents[1] / path
    return path.resolve()


def load_created_projects(config: Any) -> list[dict[str, Any]]:
    path = project_store_path(config)
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"project registry cannot be read: {path}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RuntimeError("project registry must contain a list of project objects")
    return [dict(item) for item in value]


def append_created_project(config: Any, project: dict[str, Any]) -> None:
    path = project_store_path(config)
    values = load_created_projects(config)
    if any(str(item.get("id", "")) == str(project.get("id", "")) for item in values):
        raise RuntimeError(f"project already exists: {project.get('id', '')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        redact_sensitive_data([*values, dict(project)]),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    _atomic_write_text(path, f"{content}\n")


def default_project_validation() -> dict[str, Any]:
    """Validation suitable for a newly-created small Python project."""

    return {
        "required_paths": ["tests"],
        "dependency_paths": [],
        "preflight": [],
        "test_groups": [
            {
                "name": "python-tests",
                "root": "tests",
                "path_base": ".",
                "suffixes": [".py"],
                "command": ["python3", "-m", "unittest", "{tests}", "-v"],
            }
        ],
        "full_commands": [
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
        ],
    }


__all__ = [
    "append_created_project",
    "default_project_validation",
    "load_created_projects",
    "project_store_path",
]
