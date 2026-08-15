"""Persistent project registration for projects created from the web UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codex_loop.state import _atomic_write_text, redact_sensitive_data


PROJECT_TYPES = {"python", "frontend", "fullstack"}


VALIDATION_OPTIONS = {
    "python_tests": {
        "required_paths": ["tests"],
        "test_group": {
            "name": "python-tests",
            "root": "tests",
            "path_base": ".",
            "suffixes": [".py"],
            "command": ["python3", "-m", "unittest", "{tests}", "-v"],
        },
        "full_command": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
        "preflight": [],
    },
    "frontend_tests": {
        "required_paths": ["frontend/package.json"],
        "dependency_paths": ["frontend/node_modules"],
        "test_group": {
            "name": "frontend-tests",
            "root": "frontend",
            "path_base": "frontend",
            "suffixes": [".test.ts", ".spec.ts"],
            "command": ["npm", "--prefix", "frontend", "test", "--", "{tests}"],
        },
        "full_command": ["npm", "--prefix", "frontend", "test"],
        "preflight": [
            {
                "command": ["npm", "--version"],
                "unavailable_message": "Node/npm runtime is unavailable",
            }
        ],
    },
    "frontend_typecheck": {
        "required_paths": ["frontend/package.json"],
        "dependency_paths": ["frontend/node_modules"],
        "full_command": ["npm", "--prefix", "frontend", "run", "typecheck"],
        "preflight": [
            {
                "command": ["npm", "--version"],
                "unavailable_message": "Node/npm runtime is unavailable",
            }
        ],
    },
    "frontend_build": {
        "required_paths": ["frontend/package.json"],
        "dependency_paths": ["frontend/node_modules"],
        "full_command": ["npm", "--prefix", "frontend", "run", "build"],
        "preflight": [
            {
                "command": ["npm", "--version"],
                "unavailable_message": "Node/npm runtime is unavailable",
            }
        ],
    },
}


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


def remove_created_project(config: Any, project_id: str) -> dict[str, Any]:
    """Remove one web-created project registration without touching its directory."""

    normalized_id = str(project_id).strip()
    path = project_store_path(config)
    values = load_created_projects(config)
    kept: list[dict[str, Any]] = []
    removed: dict[str, Any] | None = None
    for item in values:
        if str(item.get("id", "")).strip() == normalized_id:
            removed = dict(item)
        else:
            kept.append(item)
    if removed is None:
        raise KeyError(normalized_id)
    content = json.dumps(
        redact_sensitive_data(kept),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    _atomic_write_text(path, f"{content}\n")
    return removed


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


def validation_for_project(
    project_type: str,
    selected_options: list[str] | None = None,
    python_env_name: str | None = None,
) -> dict[str, Any]:
    """Build a trusted validation profile from project type and known options."""

    normalized_type = str(project_type).strip().lower()
    if normalized_type not in PROJECT_TYPES:
        raise ValueError(f"unsupported project_type: {normalized_type}")
    defaults = {
        "python": ["python_tests"],
        "frontend": ["frontend_tests"],
        "fullstack": ["python_tests", "frontend_tests"],
    }
    selected = list(defaults[normalized_type] if selected_options is None else selected_options)
    allowed = {
        "python": {"python_tests"},
        "frontend": {"frontend_tests", "frontend_typecheck", "frontend_build"},
        "fullstack": {"python_tests", "frontend_tests", "frontend_typecheck", "frontend_build"},
    }[normalized_type]
    unknown = sorted(set(selected) - allowed)
    if unknown:
        raise ValueError(
            f"validation options are not available for {normalized_type}: {', '.join(unknown)}"
        )
    required = set(defaults[normalized_type])
    missing = sorted(required - set(selected))
    if missing:
        raise ValueError(
            f"required validation options are missing for {normalized_type}: {', '.join(missing)}"
        )

    required_paths: list[str] = []
    dependency_paths: list[str] = []
    preflight: list[dict[str, Any]] = []
    test_groups: list[dict[str, Any]] = []
    full_commands: list[list[str]] = []
    for option in selected:
        definition = VALIDATION_OPTIONS[option]
        required_paths.extend(definition.get("required_paths", []))
        dependency_paths.extend(definition.get("dependency_paths", []))
        preflight.extend(definition.get("preflight", []))
        if "test_group" in definition:
            group = dict(definition["test_group"])
            if option == "python_tests" and python_env_name:
                group["command"] = [
                    "conda", "run", "-n", python_env_name,
                    "pytest", "-q", "{tests}",
                ]
            test_groups.append(group)
        full_command = definition.get("full_command")
        if full_command:
            full_commands.append(
                [
                    "conda", "run", "-n", python_env_name, "pytest", "-q", "tests"
                ]
                if option == "python_tests" and python_env_name
                else full_command
            )
        if option == "python_tests" and python_env_name:
            preflight.append(
                {
                    "command": [
                        "conda", "run", "-n", python_env_name,
                        "python", "-c", "import pytest",
                    ],
                    "unavailable_message": "项目 Conda 环境或 pytest 不可用",
                }
            )
    return {
        "required_paths": list(dict.fromkeys(required_paths)),
        "dependency_paths": list(dict.fromkeys(dependency_paths)),
        "preflight": _unique_mappings(preflight),
        "test_groups": test_groups,
        "full_commands": full_commands,
    }


def _unique_mappings(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


__all__ = [
    "append_created_project",
    "remove_created_project",
    "default_project_validation",
    "PROJECT_TYPES",
    "VALIDATION_OPTIONS",
    "validation_for_project",
    "load_created_projects",
    "project_store_path",
]
