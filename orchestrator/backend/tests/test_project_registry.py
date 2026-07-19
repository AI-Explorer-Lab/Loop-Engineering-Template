from __future__ import annotations

from pathlib import Path
from threading import BoundedSemaphore, Event

import pytest

from orchestrator.backend.exceptions.business_exception import ProjectNotFoundError
from orchestrator.backend.service.project_registry import ProjectRegistry
from orchestrator.backend.utils.task_executor import TaskExecutor


def _config(first: Path, second: Path) -> dict[str, object]:
    return {
        "agent": {
            "validation_timeout_seconds": 30,
            "max_parallel_projects": 1,
            "projects": [
                {"id": "first", "name": "First", "repo_root": str(first), "default": True},
                {"id": "second", "name": "Second", "repo_root": str(second), "default": False},
            ],
        }
    }


def test_registry_isolates_projects_but_shares_one_executor_per_project(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    registry = ProjectRegistry(_config(first, second))
    try:
        assert registry.default.project_id == "first"
        assert registry.get("first").repo_root == first
        assert (
            registry.get("first").task_service.executor
            is registry.get("first").queue_service.executor
        )
        assert (
            registry.get("first").task_service.executor
            is not registry.get("second").task_service.executor
        )
        with pytest.raises(ProjectNotFoundError):
            registry.get("outside")
    finally:
        registry.close(wait=True)


def test_global_gate_limits_parallel_work_across_project_executors() -> None:
    gate = BoundedSemaphore(1)
    first_executor = TaskExecutor(global_gate=gate)
    second_executor = TaskExecutor(global_gate=gate)
    first_started = Event()
    second_started = Event()
    release_first = Event()

    def first_operation() -> str:
        first_started.set()
        assert release_first.wait(timeout=5)
        return "first"

    def second_operation() -> str:
        second_started.set()
        return "second"

    try:
        first = first_executor.submit_operation("first", first_operation)
        assert first_started.wait(timeout=5)
        second = second_executor.submit_operation("second", second_operation)
        assert not second_started.wait(timeout=0.1)
        release_first.set()
        assert first.future.result(timeout=5) == "first"
        assert second.future.result(timeout=5) == "second"
        assert second_started.is_set()
    finally:
        release_first.set()
        first_executor.shutdown(wait=True)
        second_executor.shutdown(wait=True)
