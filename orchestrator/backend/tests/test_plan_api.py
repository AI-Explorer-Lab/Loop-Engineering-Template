from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.backend.controller.plan_api import router
from orchestrator.backend.exceptions.exception_handler import register_exception_handlers


class FakePlanService:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None
        self.confirmed: dict[str, Any] | None = None
        self.create_had_running_loop: bool | None = None
        self.confirm_had_running_loop: bool | None = None

    def create(
        self,
        *,
        name: str,
        requirement: str,
        acceptance_criteria: list[str],
    ) -> dict[str, Any]:
        self.create_had_running_loop = _has_running_loop()
        self.created = {
            "name": name,
            "requirement": requirement,
            "acceptance_criteria": acceptance_criteria,
        }
        return {
            "plan_id": "plan-fixture",
            "status": "ready",
            "execution_mode": "single",
            "subtasks": [],
        }

    @staticmethod
    def get(plan_id: str) -> dict[str, Any]:
        return {"plan_id": plan_id, "status": "ready"}

    def confirm(
        self,
        plan_id: str,
        *,
        reviewer: str,
        edited_draft: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.confirm_had_running_loop = _has_running_loop()
        self.confirmed = {
            "plan_id": plan_id,
            "reviewer": reviewer,
            "edited_draft": edited_draft,
        }
        return {
            "plan_id": plan_id,
            "target_kind": "task",
            "target": {"task_id": "task-fixture"},
            "confirmation": {"reviewer": reviewer},
        }


class FakeRegistry:
    def __init__(self, service: FakePlanService) -> None:
        self.context = SimpleNamespace(plan_service=service)

    def get(self, _project_id: str | None = None) -> Any:
        return self.context


def client(service: FakePlanService) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.project_registry = FakeRegistry(service)
    app.include_router(router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_plan_preview_is_created_without_starting_a_target() -> None:
    service = FakePlanService()
    with client(service) as api:
        response = api.post(
            "/api/plans",
            json={
                "name": "Filtering",
                "requirement": "Add filtering",
                "acceptance_criteria": ["Filtering works"],
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["plan_id"] == "plan-fixture"
    assert service.created == {
        "name": "Filtering",
        "requirement": "Add filtering",
        "acceptance_criteria": ["Filtering works"],
    }
    assert service.confirmed is None
    assert service.create_had_running_loop is False


def test_plan_confirmation_carries_the_human_edited_draft() -> None:
    service = FakePlanService()
    edited = {"plan_id": "plan-fixture", "status": "ready", "name": "Edited"}
    with client(service) as api:
        fetched = api.get("/api/plans/plan-fixture")
        response = api.post(
            "/api/plans/plan-fixture/confirm",
            json={"reviewer": "Planner Reviewer", "edited_draft": edited},
        )

    assert fetched.status_code == 200
    assert response.status_code == 202
    assert response.json()["data"]["target_kind"] == "task"
    assert service.confirmed == {
        "plan_id": "plan-fixture",
        "reviewer": "Planner Reviewer",
        "edited_draft": edited,
    }
    assert service.confirm_had_running_loop is False
