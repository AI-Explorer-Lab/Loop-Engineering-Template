"""Independent specification and architecture evaluation over frozen inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .context import ContextSnapshot
from .models import InfrastructureError, TaskSpec, utc_now_iso
from .role_runner import StructuredRoleRunner
from .state import _atomic_write_json, redact_sensitive_data, sanitize_for_codex
from .validation_evidence import ValidationEvidenceSnapshot


EvaluationStatus = Literal[
    "pass", "fail", "needs_human", "not_applicable", "not_evaluated"
]


class EvaluationEvidence(BaseModel):
    """Non-validation evidence supplied by an evaluator."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["changed_file", "diff"]
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
    validation_evidence_ids: list[
        Annotated[str, Field(pattern=r"^VAL-[0-9]{3}$", max_length=7)]
    ] = Field(
        min_length=0,
        max_length=100,
    )
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
    changed_location: str = Field(
        default="",
        max_length=1000,
        description=(
            "Exactly one path copied verbatim from changed_files, optionally followed "
            "by a colon and a location within that same file."
        ),
    )
    knowledge: KnowledgeCitation


class ArchitectureEvaluationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EvaluationStatus
    findings: list[ArchitectureFinding] = Field(default_factory=list, max_length=50)
    summary: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def validate_findings(self) -> "ArchitectureEvaluationOutput":
        if self.status == "not_evaluated" and self.findings:
            raise ValueError(
                "not_evaluated architecture output cannot contain findings"
            )
        if self.status in {"fail", "needs_human"} and not self.findings:
            raise ValueError(
                "failing or human-required architecture output needs a finding"
            )
        if self.status == "pass" and any(
            item.status != "pass" for item in self.findings
        ):
            raise ValueError("passing architecture output conflicts with its findings")
        if self.status == "fail" and not any(
            item.status == "fail" for item in self.findings
        ):
            raise ValueError("failing architecture output requires a failing finding")
        if self.status == "needs_human" and not any(
            item.status == "needs_human" for item in self.findings
        ):
            raise ValueError(
                "human-required architecture output needs a human-required finding"
            )
        if self.status == "not_applicable" and self.findings:
            raise ValueError(
                "not_applicable architecture output cannot contain findings"
            )
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

    @staticmethod
    def input_binding(
        *,
        task: TaskSpec,
        context: ContextSnapshot,
        changed_files: list[Mapping[str, Any]],
        diff_text: str,
        validation_evidence: ValidationEvidenceSnapshot,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Verify and bind every trusted input used by both evaluator roles."""

        context.verify_hash()
        validation_evidence.verify_hash()
        if validation_evidence.task_id != task.task_id:
            raise InfrastructureError("validation evidence belongs to a different task")
        if validation_evidence.status != "pass":
            raise InfrastructureError("evaluation requires passing validation evidence")
        actual_diff_sha256 = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
        if actual_diff_sha256 != validation_evidence.final_diff_sha256:
            raise InfrastructureError(
                "evaluation diff does not match validation evidence"
            )
        normalized_files = _normalize_changed_files(changed_files)
        changed_files_sha256 = _json_sha(normalized_files)
        binding = {
            "task_id": task.task_id,
            "base_commit": validation_evidence.base_commit,
            "validation_round": validation_evidence.validation_round,
            "validation_evidence_sha256": validation_evidence.snapshot_sha256,
            "final_diff_sha256": validation_evidence.final_diff_sha256,
            "context_sha256": context.snapshot_sha256,
            "changed_files_sha256": changed_files_sha256,
        }
        binding["evaluation_input_sha256"] = _json_sha(binding)
        return binding, normalized_files

    def evaluate(
        self,
        *,
        task: TaskSpec,
        context: ContextSnapshot,
        changed_files: list[Mapping[str, Any]],
        diff_text: str,
        validation_evidence: ValidationEvidenceSnapshot,
        validation_evidence_path: str,
        artifact_root: str | Path,
    ) -> dict[str, Any]:
        root = Path(artifact_root)
        validation_round = validation_evidence.validation_round
        round_root = root / f"round-{validation_round:02d}"
        criteria = {
            f"AC-{index:03d}": value
            for index, value in enumerate(task.acceptance_criteria, start=1)
        }
        binding, normalized_files = self.input_binding(
            task=task,
            context=context,
            changed_files=changed_files,
            diff_text=diff_text,
            validation_evidence=validation_evidence,
        )
        shared = {
            "task": {
                "task_id": task.task_id,
                "requirement": task.requirement,
                "acceptance_criteria": criteria,
            },
            "evaluation_binding": binding,
            "validation_evidence": validation_evidence.to_dict(),
            "changed_files": normalized_files,
            "diff_excerpt": sanitize_for_codex(diff_text, max_chars=24000),
            "frozen_context": context.to_dict(),
        }
        prompt = json.dumps(shared, ensure_ascii=False, sort_keys=True)
        spec_run = self.role_runner.run(
            role="spec_evaluator",
            prompt=prompt,
            output_model=SpecEvaluationOutput,
            artifact_dir=round_root / "spec-role",
        )
        spec = SpecEvaluationOutput.model_validate(
            redact_sensitive_data(spec_run.output.model_dump(mode="json"))
        )
        self._validate_spec(
            spec,
            criteria,
            context,
            validation_evidence,
            normalized_files,
        )

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
                prompt=prompt,
                output_model=ArchitectureEvaluationOutput,
                artifact_dir=round_root / "architecture-role",
            )
            architecture = ArchitectureEvaluationOutput.model_validate(
                redact_sensitive_data(architecture_run.output.model_dump(mode="json"))
            )
            architecture_thread_id = architecture_run.thread_id
            self._validate_architecture(
                architecture,
                context,
                normalized_files,
            )

        common = {
            "schema_version": 2,
            **binding,
            "validation_evidence_path": validation_evidence_path,
        }
        spec_value = {
            **common,
            "role_thread_id": spec_run.thread_id,
            "output": spec.model_dump(mode="json"),
            "created_at": utc_now_iso(),
        }
        architecture_value = {
            **common,
            "role_thread_id": architecture_thread_id,
            "output": architecture.model_dump(mode="json"),
            "created_at": utc_now_iso(),
        }
        aggregate = freeze_aggregate(
            self._aggregate(
                spec=spec,
                architecture=architecture,
                context=context,
                validation_evidence=validation_evidence,
                validation_evidence_path=validation_evidence_path,
                binding=binding,
            )
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
                    "requires_human": aggregate["requires_human"],
                    "warning_count": len(aggregate["warnings"]),
                    "validation_evidence_sha256": (validation_evidence.snapshot_sha256),
                    "context_sha256": context.snapshot_sha256,
                    "evaluation_input_sha256": binding["evaluation_input_sha256"],
                    "aggregate_sha256": aggregate["aggregate_sha256"],
                },
            )
        return aggregate

    @staticmethod
    def _validate_spec(
        output: SpecEvaluationOutput,
        criteria: Mapping[str, str],
        context: ContextSnapshot,
        validation_evidence: ValidationEvidenceSnapshot,
        changed_files: list[dict[str, Any]],
    ) -> None:
        ids = [item.acceptance_id for item in output.criteria]
        if len(ids) != len(set(ids)) or set(ids) != set(criteria):
            raise InfrastructureError(
                "spec evaluator must return every original acceptance id exactly once"
            )
        known_validation = {item.evidence_id for item in validation_evidence.commands}
        known_paths = {
            str(item.get("path", "")) for item in changed_files if item.get("path")
        }
        for criterion in output.criteria:
            validation_ids = criterion.validation_evidence_ids
            if len(validation_ids) != len(set(validation_ids)):
                raise InfrastructureError(
                    "spec evaluator returned duplicate validation evidence ids"
                )
            if any(item not in known_validation for item in validation_ids):
                raise InfrastructureError(
                    "spec evaluator cited validation evidence outside the frozen snapshot"
                )
            supported = bool(
                validation_ids or criterion.evidence or criterion.knowledge_citations
            )
            if criterion.status in {"pass", "fail"} and not supported:
                raise InfrastructureError(
                    "passing or failing specification results require cited evidence"
                )
            if (
                criterion.status == "fail"
                and validation_ids
                and not criterion.evidence
                and not criterion.knowledge_citations
            ):
                raise InfrastructureError(
                    "passing command evidence alone cannot support a specification failure"
                )
            for evidence in criterion.evidence:
                source_path = evidence.source.split(":", 1)[0]
                location_path = evidence.location.split(":", 1)[0]
                if (
                    evidence.kind == "changed_file"
                    and source_path not in known_paths
                    and location_path not in known_paths
                ):
                    raise InfrastructureError(
                        "spec evaluator cited a file outside the frozen changed-file set"
                    )
        EvaluationCoordinator._validate_citations(
            [
                citation
                for item in output.criteria
                for citation in item.knowledge_citations
            ],
            context,
        )

    @staticmethod
    def _validate_architecture(
        output: ArchitectureEvaluationOutput,
        context: ContextSnapshot,
        changed_files: list[dict[str, Any]],
    ) -> None:
        EvaluationCoordinator._validate_citations(
            [item.knowledge for item in output.findings], context
        )
        identifiers = [item.finding_id for item in output.findings]
        if len(identifiers) != len(set(identifiers)):
            raise InfrastructureError("architecture finding_id values must be unique")
        known_paths = {
            str(item.get("path", "")) for item in changed_files if item.get("path")
        }
        for finding in output.findings:
            location_path = finding.changed_location.split(":", 1)[0]
            if location_path not in known_paths:
                raise InfrastructureError(
                    "architecture evaluator cited a location outside changed files"
                )

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
    def verify_frozen_evaluation(
        *,
        task: TaskSpec,
        context: ContextSnapshot,
        changed_files: list[Mapping[str, Any]],
        validation_evidence: ValidationEvidenceSnapshot,
        validation_evidence_path: str,
        binding: Mapping[str, Any],
        spec_artifact: Mapping[str, Any],
        architecture_artifact: Mapping[str, Any],
        aggregate: Mapping[str, Any],
    ) -> None:
        """Re-validate role outputs and deterministically reproduce aggregation."""

        verify_aggregate_binding(
            aggregate,
            binding=binding,
            evidence_path=validation_evidence_path,
            evidence_ids=[item.evidence_id for item in validation_evidence.commands],
        )
        for artifact in (spec_artifact, architecture_artifact):
            verify_evaluation_artifact_binding(
                artifact,
                binding=binding,
                evidence_path=validation_evidence_path,
            )
        try:
            spec = SpecEvaluationOutput.model_validate(spec_artifact.get("output"))
            architecture = ArchitectureEvaluationOutput.model_validate(
                architecture_artifact.get("output")
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise InfrastructureError(
                "frozen evaluation role output is invalid"
            ) from exc
        criteria = {
            f"AC-{index:03d}": value
            for index, value in enumerate(task.acceptance_criteria, start=1)
        }
        normalized_files = _normalize_changed_files(changed_files)
        EvaluationCoordinator._validate_spec(
            spec,
            criteria,
            context,
            validation_evidence,
            normalized_files,
        )
        EvaluationCoordinator._validate_architecture(
            architecture,
            context,
            normalized_files,
        )
        created_at = aggregate.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise InfrastructureError("evaluation aggregate created_at is missing")
        expected = freeze_aggregate(
            EvaluationCoordinator._aggregate(
                spec=spec,
                architecture=architecture,
                context=context,
                validation_evidence=validation_evidence,
                validation_evidence_path=validation_evidence_path,
                binding=binding,
                created_at=created_at,
            )
        )
        if dict(aggregate) != expected:
            raise InfrastructureError(
                "evaluation aggregate semantic projection changed"
            )

    @staticmethod
    def _aggregate(
        *,
        spec: SpecEvaluationOutput,
        architecture: ArchitectureEvaluationOutput,
        context: ContextSnapshot,
        validation_evidence: ValidationEvidenceSnapshot,
        validation_evidence_path: str,
        binding: Mapping[str, Any],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        knowledge = {
            (item.knowledge_id, item.revision, item.path): item
            for item in context.knowledge
        }
        blocking: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        information: list[dict[str, Any]] = []
        requires_human = False
        for criterion in spec.criteria:
            value = criterion.model_dump(mode="json")
            if criterion.status == "fail":
                blocking.append({"layer": "specification", **value})
            elif criterion.status in {"needs_human", "not_evaluated"}:
                requires_human = True
                warnings.append({"layer": "specification", **value})
            elif criterion.status == "not_applicable":
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
                if finding.status == "needs_human":
                    requires_human = True
                warnings.append(
                    {
                        "layer": "architecture",
                        "constraint_strength": item.constraint_strength,
                        **value,
                    }
                )
            elif finding.status == "not_evaluated":
                information.append({"layer": "architecture", **value})
        if architecture.status == "needs_human":
            requires_human = True
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
        evidence_ids = [item.evidence_id for item in validation_evidence.commands]
        return {
            "schema_version": 2,
            "validation_round": validation_evidence.validation_round,
            "validation": {
                "status": validation_evidence.status,
                "evidence_ids": evidence_ids,
                "evidence_path": validation_evidence_path,
            },
            "syntax": {
                "status": "pass",
                "source": "frozen validation evidence",
                "evidence_ids": evidence_ids,
            },
            "logic": {
                "status": "pass",
                "source": "frozen validation evidence",
                "evidence_ids": evidence_ids,
            },
            "specification": {"status": _layer_status(spec.criteria)},
            "architecture": {"status": architecture.status},
            "requires_repair": bool(blocking),
            "requires_human": requires_human,
            "blocking_findings": blocking,
            "warnings": warnings,
            "information": information,
            **dict(binding),
            "created_at": created_at or utc_now_iso(),
        }


def build_legacy_aggregate(
    evidence: ValidationEvidenceSnapshot,
    *,
    evidence_path: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create an explicit non-passing projection for an unbound legacy run."""

    if evidence.status != "legacy_evidence_unavailable":
        raise ValueError("legacy aggregate requires a legacy evidence marker")
    return freeze_aggregate(
        {
            "schema_version": 2,
            "task_id": evidence.task_id,
            "base_commit": evidence.base_commit,
            "validation_round": evidence.validation_round,
            "validation": {
                "status": evidence.status,
                "evidence_ids": [],
                "evidence_path": evidence_path,
            },
            "syntax": {"status": "not_evaluated", "source": "legacy run"},
            "logic": {"status": "not_evaluated", "source": "legacy run"},
            "specification": {"status": "not_evaluated"},
            "architecture": {"status": "not_evaluated"},
            "requires_repair": False,
            "requires_human": True,
            "blocking_findings": [],
            "warnings": [
                {
                    "layer": "validation",
                    "status": evidence.status,
                    "message": "Historical validation evidence is unavailable.",
                }
            ],
            "information": [],
            "validation_evidence_sha256": evidence.snapshot_sha256,
            "final_diff_sha256": evidence.final_diff_sha256,
            "context_sha256": "",
            "changed_files_sha256": "",
            "evaluation_input_sha256": "",
            "created_at": created_at or utc_now_iso(),
        }
    )


def build_non_passing_aggregate(
    evidence: ValidationEvidenceSnapshot,
    *,
    evidence_path: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Project failed or infrastructure validation without invoking a model."""

    if evidence.status not in {"fail", "infra_error"}:
        raise ValueError("non-passing aggregate requires failed validation evidence")
    evidence_ids = [item.evidence_id for item in evidence.commands]
    return freeze_aggregate(
        {
            "schema_version": 2,
            "task_id": evidence.task_id,
            "base_commit": evidence.base_commit,
            "validation_round": evidence.validation_round,
            "validation": {
                "status": evidence.status,
                "evidence_ids": evidence_ids,
                "evidence_path": evidence_path,
            },
            "syntax": {
                "status": "not_evaluated",
                "source": "non-passing frozen validation evidence",
                "evidence_ids": evidence_ids,
            },
            "logic": {
                "status": "not_evaluated",
                "source": "non-passing frozen validation evidence",
                "evidence_ids": evidence_ids,
            },
            "specification": {"status": "not_evaluated"},
            "architecture": {"status": "not_evaluated"},
            "requires_repair": evidence.status == "fail",
            "requires_human": evidence.status == "infra_error",
            "blocking_findings": [],
            "warnings": [
                {
                    "layer": "validation",
                    "status": evidence.status,
                    "message": "Fixed validation did not pass; evaluators were not run.",
                }
            ],
            "information": [],
            "validation_evidence_sha256": evidence.snapshot_sha256,
            "final_diff_sha256": evidence.final_diff_sha256,
            "context_sha256": "",
            "changed_files_sha256": "",
            "evaluation_input_sha256": "",
            "created_at": created_at or utc_now_iso(),
        }
    )


def freeze_aggregate(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add a deterministic self-hash to a complete aggregate projection."""

    redacted = redact_sensitive_data(dict(value))
    if not isinstance(redacted, dict):
        raise TypeError("evaluation aggregate must be an object")
    frozen = redacted
    frozen.pop("aggregate_sha256", None)
    frozen["aggregate_sha256"] = _json_sha(frozen)
    return frozen


def _verify_aggregate_hash(aggregate: Mapping[str, Any]) -> None:
    expected = aggregate.get("aggregate_sha256")
    payload = dict(aggregate)
    payload.pop("aggregate_sha256", None)
    if not isinstance(expected, str) or expected != _json_sha(payload):
        raise InfrastructureError("evaluation aggregate hash changed")


def verify_control_aggregate(
    aggregate: Mapping[str, Any],
    *,
    evidence: ValidationEvidenceSnapshot,
    evidence_path: str,
) -> None:
    """Verify a legacy or non-passing aggregate without model outputs."""

    created_at = aggregate.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise InfrastructureError("control aggregate created_at is missing")
    if evidence.status == "legacy_evidence_unavailable":
        expected = build_legacy_aggregate(
            evidence,
            evidence_path=evidence_path,
            created_at=created_at,
        )
    elif evidence.status in {"fail", "infra_error"}:
        expected = build_non_passing_aggregate(
            evidence,
            evidence_path=evidence_path,
            created_at=created_at,
        )
    else:
        raise InfrastructureError("control aggregate cannot represent passing evidence")
    if dict(aggregate) != expected:
        raise InfrastructureError("control aggregate projection changed")


def verify_aggregate_binding(
    aggregate: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    evidence_path: str,
    evidence_ids: list[str],
) -> None:
    """Reject a stale or tampered round-specific evaluation aggregate."""

    _verify_aggregate_hash(aggregate)
    if int(aggregate.get("schema_version", 0)) != 2:
        raise InfrastructureError("evaluation aggregate schema is stale")
    if not isinstance(aggregate.get("requires_repair"), bool) or not isinstance(
        aggregate.get("requires_human"), bool
    ):
        raise InfrastructureError("evaluation aggregate decision fields are invalid")
    for key in ("blocking_findings", "warnings", "information"):
        if not isinstance(aggregate.get(key), list):
            raise InfrastructureError(f"evaluation aggregate {key} must be an array")
    for key, expected in binding.items():
        if aggregate.get(key) != expected:
            raise InfrastructureError(f"evaluation aggregate {key} binding changed")
    validation = aggregate.get("validation")
    if not isinstance(validation, Mapping):
        raise InfrastructureError("evaluation aggregate has no validation binding")
    if (
        validation.get("status") != "pass"
        or validation.get("evidence_path") != evidence_path
        or validation.get("evidence_ids") != evidence_ids
    ):
        raise InfrastructureError("evaluation aggregate validation binding changed")


def verify_evaluation_artifact_binding(
    artifact: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    evidence_path: str,
) -> None:
    """Verify one immutable role artifact targets the same frozen input."""

    if int(artifact.get("schema_version", 0)) != 2:
        raise InfrastructureError("evaluation role artifact schema is stale")
    if not isinstance(artifact.get("output"), Mapping):
        raise InfrastructureError("evaluation role artifact output is missing")
    for key, expected in binding.items():
        if artifact.get(key) != expected:
            raise InfrastructureError(f"evaluation role artifact {key} binding changed")
    if artifact.get("validation_evidence_path") != evidence_path:
        raise InfrastructureError(
            "evaluation role artifact evidence path binding changed"
        )


def _normalize_changed_files(
    values: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [
        {str(key): item for key, item in dict(value).items()} for value in values
    ]
    normalized.sort(key=lambda item: str(item.get("path", "")))
    paths = [str(item.get("path", "")) for item in normalized]
    if any(not path for path in paths) or len(paths) != len(set(paths)):
        raise InfrastructureError("changed files must have unique non-empty paths")
    return normalized


def _layer_status(criteria: list[SpecCriterionResult]) -> str:
    statuses = [item.status for item in criteria]
    for value in ("fail", "needs_human", "not_evaluated", "pass", "not_applicable"):
        if value in statuses:
            return value
    return "not_evaluated"


def _json_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ArchitectureEvaluationOutput",
    "ArchitectureFinding",
    "EvaluationCoordinator",
    "EvaluationEvidence",
    "KnowledgeCitation",
    "SpecCriterionResult",
    "SpecEvaluationOutput",
    "build_legacy_aggregate",
    "build_non_passing_aggregate",
    "freeze_aggregate",
    "verify_aggregate_binding",
    "verify_control_aggregate",
    "verify_evaluation_artifact_binding",
]
