from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.backend.config.config import (
    REPO_ROOT,
    knowledge_from_settings,
    projects_from_settings,
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
