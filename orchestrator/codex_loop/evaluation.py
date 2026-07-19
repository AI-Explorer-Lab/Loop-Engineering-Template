"""Independent specification and architecture evaluation over frozen inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import ContextSnapshot
from .models import InfrastructureError, TaskSpec, utc_now_iso
from .role_runner import StructuredRoleRunner
from .state import _atomic_write_json, sanitize_for_codex


EvaluationStatus = Literal[
    "pass", "fail", "needs_human", "not_applicable", "not_evaluated"
]


class EvaluationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["test", "changed_file", "diff", "knowledge"]
    source: str = Field(min_length=1, max_length=500)
    location: str = Field(default="", max_length=1000)
    detail: str = Field(min_length=1, max_length=4000)


class KnowledgeCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_id: str = Field(min_length=1, max_length=300)
    revision: int = Field(ge=1)
    path: str = Field(min_length=1, max_length=1000)


class SpecCriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acceptance_id: str = Field(pattern=r"^AC-[0-9]{3}$")
    status: EvaluationStatus
    rationale: str = Field(min_length=1, max_length=8000)
    evidence: list[EvaluationEvidence] = Field(default_factory=list, max_length=30)
    knowledge_citations: list[KnowledgeCitation] = Field(
        default_factory=list, max_length=20
    )


class SpecEvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[SpecCriterionResult] = Field(min_length=1, max_length=50)
    summary: str = Field(min_length=1, max_length=8000)


class ArchitectureFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1, max_length=100)
    status: EvaluationStatus
    rationale: str = Field(min_length=1, max_length=8000)
    changed_location: str = Field(default="", max_length=1000)
    knowledge: KnowledgeCitation


class ArchitectureEvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EvaluationStatus
    findings: list[ArchitectureFinding] = Field(default_factory=list, max_length=50)
    summary: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def validate_findings(self) -> "ArchitectureEvaluationOutput":
        if self.status == "not_evaluated" and self.findings:
            raise ValueError("not_evaluated architecture output cannot contain findings")
        return self


class EvaluationCoordinator:
    """Run role-isolated evaluators and apply deterministic aggregation rules."""

    def __init__(
        self,
        role_runner: StructuredRoleRunner,
        *,
        event_sink: Any | None = None,
    ) -> None:
        self.role_runner = role_runner
        self.event_sink = event_sink

    def evaluate(
        self,
        *,
        task: TaskSpec,
        context: ContextSnapshot,
        changed_files: list[Mapping[str, Any]],
        diff_text: str,
        validation_round: int,
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        root = Path(artifact_root)
        round_root = root / f"round-{validation_round:02d}"
        criteria = {
            f"AC-{index:03d}": value
            for index, value in enumerate(task.acceptance_criteria, start=1)
        }
        shared = {
            "task": {
                "task_id": task.task_id,
                "requirement": task.requirement,
                "acceptance_criteria": criteria,
            },
            "validation_round": validation_round,
            "changed_files": [dict(item) for item in changed_files],
            "diff_excerpt": sanitize_for_codex(diff_text, max_chars=24000),
            "frozen_context": context.to_dict(),
        }
        spec_run = self.role_runner.run(
            role="spec_evaluator",
            prompt=json.dumps(shared, ensure_ascii=False, sort_keys=True),
            output_model=SpecEvaluationOutput,
            artifact_dir=round_root / "spec-role",
        )
        spec = SpecEvaluationOutput.model_validate(spec_run.output.model_dump())
        self._validate_spec(spec, criteria, context)

        architecture_knowledge = [
            item
            for item in context.knowledge
            if item.knowledge_type in {"decision", "model", "guideline", "pitfall"}
        ]
        if not architecture_knowledge:
            architecture = ArchitectureEvaluationOutput(
                status="not_evaluated",
                findings=[],
                summary="No applicable frozen architecture knowledge was available.",
            )
            architecture_thread_id: str | None = None
        else:
            architecture_run = self.role_runner.run(
                role="architecture_evaluator",
                prompt=json.dumps(shared, ensure_ascii=False, sort_keys=True),
                output_model=ArchitectureEvaluationOutput,
                artifact_dir=round_root / "architecture-role",
            )
            architecture = ArchitectureEvaluationOutput.model_validate(
                architecture_run.output.model_dump()
            )
            architecture_thread_id = architecture_run.thread_id
            self._validate_architecture(architecture, context)

        spec_value = {
            "schema_version": 1,
            "role_thread_id": spec_run.thread_id,
            "validation_round": validation_round,
            "context_sha256": context.snapshot_sha256,
            "output": spec.model_dump(mode="json"),
            "created_at": utc_now_iso(),
        }
        architecture_value = {
            "schema_version": 1,
            "role_thread_id": architecture_thread_id,
            "validation_round": validation_round,
            "context_sha256": context.snapshot_sha256,
            "output": architecture.model_dump(mode="json"),
            "created_at": utc_now_iso(),
        }
        aggregate = self._aggregate(
            spec=spec,
            architecture=architecture,
            context=context,
            validation_round=validation_round,
        )
        _atomic_write_json(round_root / "spec.json", spec_value)
        _atomic_write_json(round_root / "architecture.json", architecture_value)
        _atomic_write_json(round_root / "aggregate.json", aggregate)
        _atomic_write_json(root / "spec.json", spec_value)
        _atomic_write_json(root / "architecture.json", architecture_value)
        _atomic_write_json(root / "aggregate.json", aggregate)
        if self.event_sink is not None:
            self.event_sink(
                "evaluation.completed",
                {
                    "validation_round": validation_round,
                    "requires_repair": aggregate["requires_repair"],
                    "warning_count": len(aggregate["warnings"]),
                    "context_sha256": context.snapshot_sha256,
                    "aggregate_sha256": _json_sha(aggregate),
                },
            )
        return aggregate

    @staticmethod
    def _validate_spec(
        output: SpecEvaluationOutput,
        criteria: Mapping[str, str],
        context: ContextSnapshot,
    ) -> None:
        ids = [item.acceptance_id for item in output.criteria]
        if len(ids) != len(set(ids)) or set(ids) != set(criteria):
            raise InfrastructureError(
                "spec evaluator must return every original acceptance id exactly once"
            )
        EvaluationCoordinator._validate_citations(
            [citation for item in output.criteria for citation in item.knowledge_citations],
            context,
        )

    @staticmethod
    def _validate_architecture(
        output: ArchitectureEvaluationOutput, context: ContextSnapshot
    ) -> None:
        EvaluationCoordinator._validate_citations(
            [item.knowledge for item in output.findings], context
        )
        identifiers = [item.finding_id for item in output.findings]
        if len(identifiers) != len(set(identifiers)):
            raise InfrastructureError("architecture finding_id values must be unique")

    @staticmethod
    def _validate_citations(
        citations: list[KnowledgeCitation], context: ContextSnapshot
    ) -> None:
        known = {
            (item.knowledge_id, item.revision, item.path) for item in context.knowledge
        }
        for citation in citations:
            if (citation.knowledge_id, citation.revision, citation.path) not in known:
                raise InfrastructureError(
                    "evaluator cited knowledge outside the frozen context snapshot"
                )

    @staticmethod
    def _aggregate(
        *,
        spec: SpecEvaluationOutput,
        architecture: ArchitectureEvaluationOutput,
        context: ContextSnapshot,
        validation_round: int,
    ) -> dict[str, Any]:
        knowledge = {
            (item.knowledge_id, item.revision, item.path): item
            for item in context.knowledge
        }
        blocking: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        information: list[dict[str, Any]] = []
        for criterion in spec.criteria:
            value = criterion.model_dump(mode="json")
            if criterion.status == "fail" and criterion.evidence:
                blocking.append({"layer": "specification", **value})
            elif criterion.status == "needs_human":
                warnings.append({"layer": "specification", **value})
            elif criterion.status == "not_evaluated":
                information.append({"layer": "specification", **value})
        for finding in architecture.findings:
            citation = finding.knowledge
            item = knowledge[(citation.knowledge_id, citation.revision, citation.path)]
            value = finding.model_dump(mode="json")
            if (
                finding.status == "fail"
                and item.constraint_strength == "strong"
                and bool(finding.changed_location.strip())
            ):
                blocking.append({"layer": "architecture", **value})
            elif finding.status in {"fail", "needs_human"}:
                warnings.append(
                    {
                        "layer": "architecture",
                        "constraint_strength": item.constraint_strength,
                        **value,
                    }
                )
            elif finding.status == "not_evaluated":
                information.append({"layer": "architecture", **value})
        for item in context.knowledge:
            if item.constraint_strength == "warning":
                warnings.append(
                    {
                        "layer": "knowledge",
                        "knowledge_id": item.knowledge_id,
                        "revision": item.revision,
                        "path": item.path,
                        "message": "advisory knowledge cannot block task progression",
                    }
                )
        if architecture.status == "not_evaluated":
            information.append(
                {
                    "layer": "architecture",
                    "status": "not_evaluated",
                    "message": architecture.summary,
                }
            )
        return {
            "schema_version": 1,
            "validation_round": validation_round,
            "syntax": {"status": "pass", "source": "fixed validation commands"},
            "logic": {"status": "pass", "source": "fixed validation commands"},
            "specification": {"status": _layer_status(spec.criteria)},
            "architecture": {"status": architecture.status},
            "requires_repair": bool(blocking),
            "blocking_findings": blocking,
            "warnings": warnings,
            "information": information,
            "context_sha256": context.snapshot_sha256,
            "created_at": utc_now_iso(),
        }


def _layer_status(criteria: list[SpecCriterionResult]) -> str:
    statuses = [item.status for item in criteria]
    for value in ("fail", "needs_human", "not_evaluated", "pass", "not_applicable"):
        if value in statuses:
            return value
    return "not_evaluated"


def _json_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ArchitectureEvaluationOutput",
    "ArchitectureFinding",
    "EvaluationCoordinator",
    "EvaluationEvidence",
    "KnowledgeCitation",
    "SpecCriterionResult",
    "SpecEvaluationOutput",
]
