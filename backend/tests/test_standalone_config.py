from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.config import (
    REPO_ROOT,
    knowledge_from_settings,
    projects_from_settings,
    validate_settings,
)


def test_external_paths_are_resolved_from_the_standalone_repository() -> None:
    config = {
        "agent": {
            "knowledge": {
                "repo_root": "../Knowledge-Base",
                "mcp_registry": "../mcp/registry.json",
                "knowledge_writer_actor_id": "orchestrator",
            }
        }
    }

    knowledge = knowledge_from_settings(config)

    assert knowledge["repo_root"] == (REPO_ROOT / "../Knowledge-Base").resolve()
    assert knowledge["mcp_registry"] == (REPO_ROOT / "../mcp/registry.json").resolve()


def test_harness_configuration_is_required_without_an_enable_switch(
    tmp_path: Path,
) -> None:
    config = {
        "server": {"port": 8100, "cors_origins": ["http://localhost"]},
        "agent": {
            "validation_timeout_seconds": 30,
            "max_parallel_projects": 1,
            "projects": [
                {
                    "id": "sample",
                    "repo_root": str(tmp_path),
                    "default": True,
                    "knowledge_actor_id": "local-user",
                }
            ],
        },
    }

    with pytest.raises(RuntimeError, match=r"agent\.knowledge\.repo_root"):
        validate_settings(config)


def test_project_registry_parses_a_project_specific_validation_profile() -> None:
    config = {
        "agent": {
            "projects": [
                {
                    "id": "sample",
                    "repo_root": ".",
                    "default": True,
                    "validation": {
                        "required_paths": ["tests"],
                        "dependency_paths": ["web/node_modules"],
                        "preflight": [
                            {
                                "command": ["python", "-c", "import pytest"],
                                "unavailable_message": "Python is unavailable",
                            }
                        ],
                        "test_groups": [
                            {
                                "name": "python",
                                "root": "tests",
                                "path_base": ".",
                                "suffixes": [".py"],
                                "command": ["python", "-m", "pytest", "{tests}"],
                            }
                        ],
                        "full_commands": [["python", "-m", "pytest", "tests"]],
                    },
                }
            ]
        }
    }

    project = projects_from_settings(config)[0]

    assert project["project_id"] == "sample"
    assert project["repo_root"] == REPO_ROOT
    assert project["validation_profile"].dependency_paths == (
        Path("web/node_modules"),
    )


def test_project_registry_rejects_unsafe_validation_paths() -> None:
    config = {
        "agent": {
            "projects": [
                {
                    "id": "unsafe",
                    "repo_root": ".",
                    "default": True,
                    "validation": {
                        "required_paths": ["../outside"],
                        "full_commands": [["python", "-m", "pytest"]],
                    },
                }
            ]
        }
    }

    with pytest.raises(RuntimeError, match="safe relative path"):
        projects_from_settings(config)


def test_created_project_registry_marker_survives_config_normalization(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "projects.json"
    registry_path.write_text(
        '[{"id":"read-notes","name":"Reading Notes","repo_root":".",'
        '"default":true,"validation":{}}]\n',
        encoding="utf-8",
    )
    config = {
        "agent": {
            "project_registry_path": str(registry_path),
            "projects": [],
        }
    }

    project = projects_from_settings(config)[0]

    assert project["_created_from_registry"] is True
