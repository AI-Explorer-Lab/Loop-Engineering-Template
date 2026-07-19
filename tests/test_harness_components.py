from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_loop.context import ContextAssembler, ContextSnapshot
from codex_loop.evaluation import (
    ArchitectureEvaluationOutput,
    ArchitectureFinding,
    EvaluationCoordinator,
    KnowledgeCitation,
    SpecCriterionResult,
    SpecEvaluationOutput,
)
from codex_loop.knowledge import (
    KnowledgeItem,
    KnowledgeSelection,
)
from codex_loop.mcp_client import LocalMcpClient, McpCallError
from codex_loop.memory import MediumTermMemory
from codex_loop.models import InfrastructureError, TaskQueueSpec, TaskSpec
from codex_loop.planner import (
    PlannedSubtask,
    PlannerRoleOutput,
    PlannerService,
    append_plan_event,
)
from codex_loop.role_runner import StructuredRoleRunner


class FakeRoleRunner:
    def __init__(self, outputs: dict[str, Any]) -> None:
        self.outputs = outputs
        self.calls: list[str] = []

    def run(self, *, role: str, **_kwargs: Any) -> SimpleNamespace:
        self.calls.append(role)
        output = self.outputs[role]
        if isinstance(output, list):
            output = output.pop(0)
        return SimpleNamespace(output=output, thread_id=f"thread-{role}")


def knowledge_item(
    *,
    knowledge_id: str = "knowledge-1",
    maturity: str = "draft",
    knowledge_type: str = "guideline",
) -> KnowledgeItem:
    return KnowledgeItem(
        knowledge_id=knowledge_id,
        title="Frontend boundary",
        path=f"docs/knowledge/guidelines/{knowledge_id}.md",
        knowledge_type=knowledge_type,
        layer="layer3",
        project_id="accounting",
        scope="team",
        owner_id=None,
        maturity=maturity,
        conflict_status="none",
        revision=1,
        tags=("frontend",),
        content="Keep the frontend boundary explicit.",
        content_sha256="a" * 64,
        selection_reason="test fixture",
        stage="architecture_evaluation",
    )


class FakeKnowledgeGateway:
    def __init__(self, item: KnowledgeItem) -> None:
        self.item = item
        self.calls = 0

    def retrieve(self, *, stage: str, query: str, actor: str, **_kwargs: Any) -> KnowledgeSelection:
        self.calls += 1
        return KnowledgeSelection(
            stage=stage,
            query=query,
            actor=actor,
            catalog_sha256="b" * 64,
            items=(self.item,),
        )

    @staticmethod
    def budget(_stage: str) -> dict[str, int]:
        return {"max_catalogs": 2, "max_entries": 3, "max_chars": 4000}


class FakeSkills:
    calls = 0

    def select(self, **_kwargs: Any) -> tuple[list[Any], list[str]]:
        self.calls += 1
        return [], []


class FakeMemory:
    calls = 0

    def recall(self, **_kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return [{"task_id": "prior-task", "commit_sha": "c" * 40}]


def test_context_snapshot_is_frozen_and_tamper_evident(tmp_path: Path) -> None:
    gateway = FakeKnowledgeGateway(knowledge_item())
    skills = FakeSkills()
    memory = FakeMemory()
    assembler = ContextAssembler(gateway, skills, memory)  # type: ignore[arg-type]
    path = tmp_path / "context" / "generation.json"

    first = assembler.assemble(
        path=path,
        stage="generation",
        query="frontend",
        actor="zhangsan",
        include_memory=True,
    )
    gateway.item = knowledge_item(knowledge_id="changed-after-freeze")
    second = assembler.assemble(
        path=path,
        stage="generation",
        query="different query",
        actor="zhangsan",
        include_memory=True,
    )

    assert first == second
    assert gateway.calls == 1
    assert second.medium_term_memory[0]["task_id"] == "prior-task"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["query"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(InfrastructureError, match="hash changed"):
        assembler.assemble(
            path=path,
            stage="generation",
            query="frontend",
            actor="zhangsan",
        )


def test_medium_term_memory_uses_fixed_formula_alias_dedupe_and_stable_ties(
    tmp_path: Path,
) -> None:
    memory = MediumTermMemory(tmp_path, aliases={"前端": ["frontend"]})
    common = {
        "schema_version": 1,
        "requirement": "frontend filtering",
        "summary": "Keep frontend filters stable",
        "tags": ["filter"],
        "paths": ["frontend/src/view.ts"],
        "technologies": ["vue"],
        "review_status": "approved",
        "delivery_status": "archived",
        "commit_sha": "d" * 40,
        "committed_at": "2026-07-18T10:00:00+08:00",
    }
    memory.write_summary({**common, "task_id": "task-b"})
    memory.write_summary({**common, "task_id": "task-a", "commit_sha": "e" * 40})
    memory.write_summary(
        {
            **common,
            "task_id": "task-rejected",
            "review_status": "rejected",
        }
    )

    recalled = memory.recall(
        query="前端 frontend",
        tags=["filter"],
        paths=["frontend/src/other.ts"],
        technologies=["vue"],
    )

    assert [item["task_id"] for item in recalled] == ["task-a", "task-b"]
    assert recalled[0]["match_score"] == 10
    assert recalled[0]["score_breakdown"] == {"K": 1, "T": 1, "P": 1, "F": 1}
    with pytest.raises(ValueError, match="immutable"):
        memory.write_summary({**common, "task_id": "task-a", "summary": "changed"})


def test_planner_only_maps_original_acceptance_ids_and_waits_for_confirmation(
    tmp_path: Path,
) -> None:
    output = PlannerRoleOutput(
        execution_mode="queue",
        subtasks=[
            PlannedSubtask(
                sequence=1,
                title="Backend",
                requirement_slice="Implement backend filtering",
                source_acceptance_ids=["AC-001"],
            ),
            PlannedSubtask(
                sequence=2,
                title="Frontend",
                requirement_slice="Expose filtering in the frontend",
                source_acceptance_ids=["AC-002"],
            ),
        ],
    )
    runner = FakeRoleRunner({"planner": output})
    service = PlannerService(tmp_path, runner)  # type: ignore[arg-type]
    context = ContextSnapshot(
        stage="planner",
        query="filtering",
        actor="zhangsan",
        snapshot_sha256="f" * 64,
    )
    append_plan_event(
        tmp_path / ".codex-orchestrator/drafts/plan-automatic-001",
        "context.assembled",
        {"stage": "planner"},
    )

    draft = service.generate(
        plan_id="plan-automatic-001",
        name="Filtering",
        requirement="Add filtering end to end",
        acceptance_criteria=["API filters rows", "UI exposes the filter"],
        context=context,
    )

    assert draft.acceptance_criteria == {
        "AC-001": "API filters rows",
        "AC-002": "UI exposes the filter",
    }
    assert not (tmp_path / ".codex-orchestrator" / "runs").exists()
    assert not (tmp_path / ".codex-orchestrator" / "queues").exists()
    spec, target = service.confirm(
        draft.plan_id,
        reviewer="Planner Reviewer",
        edited_draft={
            **draft.model_dump(mode="json"),
            "name": "Filtering confirmed",
        },
    )

    assert isinstance(spec, TaskQueueSpec)
    assert [item.acceptance_criteria for item in spec.subtasks] == [
        ["API filters rows"],
        ["UI exposes the filter"],
    ]
    assert target == tmp_path / ".codex-orchestrator" / "queues" / spec.queue_id / "plan"
    assert not (tmp_path / ".codex-orchestrator" / "drafts" / draft.plan_id).exists()
    plan_events = [
        json.loads(line)
        for line in (target / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["type"] for event in plan_events] == [
        "context.assembled",
        "plan.generated",
        "plan.confirmed",
    ]
    with pytest.raises(ValueError, match="Extra inputs"):
        PlannerRoleOutput.model_validate(
            {
                **output.model_dump(mode="json"),
                "dependencies": ["invented"],
            }
        )


def evaluation_context(item: KnowledgeItem | None) -> ContextSnapshot:
    return ContextSnapshot(
        stage="evaluation",
        query="architecture",
        actor="zhangsan",
        knowledge=() if item is None else (item,),
        snapshot_sha256="1" * 64,
    )


def architecture_output(item: KnowledgeItem) -> ArchitectureEvaluationOutput:
    return ArchitectureEvaluationOutput(
        status="fail",
        findings=[
            ArchitectureFinding(
                finding_id="ARCH-001",
                status="fail",
                rationale="The changed module crosses the documented boundary.",
                changed_location="src/view.ts:12",
                knowledge=KnowledgeCitation(
                    knowledge_id=item.knowledge_id,
                    revision=item.revision,
                    path=item.path,
                ),
            )
        ],
        summary="One boundary finding.",
    )


def spec_output() -> SpecEvaluationOutput:
    return SpecEvaluationOutput(
        criteria=[
            SpecCriterionResult(
                acceptance_id="AC-001",
                status="pass",
                rationale="The fixed tests cover it.",
            )
        ],
        summary="Acceptance criterion passed.",
    )


@pytest.mark.parametrize(
    ("maturity", "requires_repair", "warning_count"),
    [("draft", False, 2), ("verified", True, 0)],
)
def test_evaluation_uses_only_strong_evidenced_knowledge_to_block(
    tmp_path: Path,
    maturity: str,
    requires_repair: bool,
    warning_count: int,
) -> None:
    item = knowledge_item(maturity=maturity)
    runner = FakeRoleRunner(
        {
            "spec_evaluator": spec_output(),
            "architecture_evaluator": architecture_output(item),
        }
    )
    aggregate = EvaluationCoordinator(runner).evaluate(  # type: ignore[arg-type]
        task=TaskSpec(
            task_id=f"evaluation-{maturity}",
            requirement="Keep architecture boundaries",
            acceptance_criteria=["The boundary remains intact"],
        ),
        context=evaluation_context(item),
        changed_files=[{"path": "src/view.ts"}],
        diff_text="+changed",
        validation_round=1,
        artifact_root=tmp_path / maturity,
    )

    assert aggregate["requires_repair"] is requires_repair
    assert len(aggregate["warnings"]) == warning_count
    assert len(aggregate["blocking_findings"]) == int(requires_repair)


def test_architecture_without_knowledge_is_not_evaluated(tmp_path: Path) -> None:
    runner = FakeRoleRunner({"spec_evaluator": spec_output()})
    aggregate = EvaluationCoordinator(runner).evaluate(  # type: ignore[arg-type]
        task=TaskSpec(
            task_id="evaluation-no-knowledge",
            requirement="Change one file",
            acceptance_criteria=["Change is present"],
        ),
        context=evaluation_context(None),
        changed_files=[{"path": "src/view.ts"}],
        diff_text="+changed",
        validation_round=1,
        artifact_root=tmp_path / "none",
    )

    assert aggregate["architecture"]["status"] == "not_evaluated"
    assert aggregate["requires_repair"] is False
    assert runner.calls == ["spec_evaluator"]


class RepairingClient:
    def __init__(self) -> None:
        self.responses = ["not json", json.dumps(spec_output().model_dump(mode="json"))]
        self.calls = 0
        self.schemas: list[dict[str, Any]] = []

    def __enter__(self) -> "RepairingClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    @staticmethod
    def start_thread() -> str:
        return "thread-repair"

    def run(self, _prompt: str, **kwargs: Any) -> SimpleNamespace:
        self.schemas.append(kwargs["output_schema"])
        value = self.responses[self.calls]
        self.calls += 1
        return SimpleNamespace(final_response=value)


def test_structured_role_allows_exactly_one_format_repair(tmp_path: Path) -> None:
    client = RepairingClient()
    runner = StructuredRoleRunner(
        tmp_path,
        client_factory=lambda _path, _role: client,
    )

    result = runner.run(
        role="spec_evaluator",
        prompt="evaluate",
        output_model=SpecEvaluationOutput,
        artifact_dir=tmp_path / "role",
    )

    assert result.repaired_format is True
    assert client.calls == 2
    assert (tmp_path / "role" / "result.json").is_file()
    _assert_strict_output_schema(client.schemas[0])


def _assert_strict_output_schema(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_strict_output_schema(item)
        return
    if not isinstance(value, dict):
        return
    assert "default" not in value
    properties = value.get("properties")
    if isinstance(properties, dict):
        assert value["required"] == list(properties)
        assert value["additionalProperties"] is False
    for item in value.values():
        _assert_strict_output_schema(item)


def test_mcp_client_rejects_cross_mode_tools_before_launch(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text("pass\n", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "server": {
                    "transport": "stdio",
                    "network": "disabled",
                    "entrypoint": "server.py",
                },
            }
        ),
        encoding="utf-8",
    )
    client = LocalMcpClient(registry, mode="read")
    with pytest.raises(McpCallError, match="unavailable in read mode"):
        client.call_tool("knowledge_create_draft", {})
