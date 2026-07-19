"""Local-first archive, medium-term memory, and idempotent knowledge outbox."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .audit import AuditRecorder
from .context import ContextSnapshot
from .mcp_client import DEFAULT_MCP_REGISTRY, LocalMcpClient, McpCallError
from .memory import MediumTermMemory
from .models import DeliveryStatus, InfrastructureError, ReviewStatus, utc_now_iso
from .role_runner import StructuredRoleRunner
from .state import StateStore, _atomic_write_json, redact_sensitive_data


SENSITIVE_PATTERN = re.compile(
    r"(?i)(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|private[_-]?key)\s*[:=]|\bsk-[A-Za-z0-9_-]{8,}\b|"
    r"\b(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+"
)
CHINESE_TEXT_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
ENGLISH_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9.+#/_-]*")


def _require_chinese_text(value: str) -> str:
    normalized = value.strip()
    if not CHINESE_TEXT_PATTERN.search(normalized):
        raise ValueError("archive knowledge text must be written in Chinese")
    return normalized


def _require_english_tags(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        tag = value.strip()
        if not ENGLISH_TAG_PATTERN.fullmatch(tag):
            raise ValueError(
                "archive tags must be lowercase English tokens containing only "
                "letters, numbers, '.', '+', '#', '/', '_' or '-'"
            )
        normalized.append(tag)
    return normalized


class ArchiveCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_type: Literal["guideline", "pitfall"]
    title: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=1000)
    problem: str = Field(min_length=1, max_length=4000)
    action: str = Field(min_length=1, max_length=4000)
    result: str = Field(min_length=1, max_length=4000)
    target_layer: Literal["layer1", "layer2", "layer3"]
    layer_reason: str = Field(min_length=1, max_length=1000)
    business_domain: str | None = Field(default=None, min_length=1, max_length=64)
    source_ids: list[str] = Field(min_length=1, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator(
        "title", "scope", "problem", "action", "result", "layer_reason"
    )
    @classmethod
    def validate_chinese_text(cls, value: str) -> str:
        return _require_chinese_text(value)

    @field_validator("tags")
    @classmethod
    def validate_english_tags(cls, values: list[str]) -> list[str]:
        return _require_english_tags(values)

    @model_validator(mode="after")
    def validate_layer_target(self) -> "ArchiveCandidate":
        if self.target_layer == "layer2" and self.business_domain is None:
            raise ValueError("Layer 2 archive candidates require business_domain")
        if self.target_layer != "layer2" and self.business_domain is not None:
            raise ValueError("business_domain is allowed only for Layer 2 candidates")
        return self

    def content(self) -> str:
        return "\n\n".join(
            [
                f"## 适用范围\n\n{self.scope}",
                f"## 问题\n\n{self.problem}",
                f"## 处理方式\n\n{self.action}",
                f"## 结果\n\n{self.result}",
                f"## 分层依据\n\n{self.layer_reason}",
            ]
        )


class ArchiveRoleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["candidates", "no_candidate"]
    task_summary: str = Field(min_length=1, max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    technologies: list[str] = Field(default_factory=list, max_length=30)
    candidates: list[ArchiveCandidate] = Field(default_factory=list, max_length=3)

    @field_validator("task_summary")
    @classmethod
    def validate_chinese_summary(cls, value: str) -> str:
        return _require_chinese_text(value)

    @field_validator("tags")
    @classmethod
    def validate_english_tags(cls, values: list[str]) -> list[str]:
        return _require_english_tags(values)

    @model_validator(mode="after")
    def validate_outcome(self) -> "ArchiveRoleOutput":
        if self.outcome == "no_candidate" and self.candidates:
            raise ValueError("no_candidate output cannot contain candidates")
        if self.outcome == "candidates" and not self.candidates:
            raise ValueError("candidates output must contain at least one candidate")
        return self


class ArchiveCoordinator:
    """Persist local truth first, then process a retryable knowledge write outbox."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        project_id: str,
        consumer_actor: str,
        writer_actor: str,
        role_runner: StructuredRoleRunner,
        memory: MediumTermMemory,
        archive_client: LocalMcpClient | None = None,
        registry_path: str | Path = DEFAULT_MCP_REGISTRY,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.project_id = str(project_id)
        self.consumer_actor = str(consumer_actor)
        self.writer_actor = str(writer_actor)
        self.role_runner = role_runner
        self.memory = memory
        self.registry_path = Path(registry_path).expanduser().resolve()
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        knowledge_settings = registry.get("knowledge", {})
        self.business_domains = {
            str(item)
            for item in (
                knowledge_settings.get("business_domains", [])
                if isinstance(knowledge_settings, Mapping)
                else []
            )
        }
        self.archive_client = archive_client or LocalMcpClient(
            self.registry_path, mode="archive"
        )

    def archive(
        self,
        task_id: str,
        *,
        store: StateStore | None = None,
        archive_context: ContextSnapshot,
    ) -> dict[str, Any]:
        run_store = store or StateStore(self.repo_root)
        state = run_store.load_state(task_id)
        task = run_store.load_task(task_id)
        if state.review_status is not ReviewStatus.APPROVED:
            raise InfrastructureError("only approved work can enter the archiver")
        if state.delivery_status not in {
            DeliveryStatus.COMMITTED,
            DeliveryStatus.ARCHIVE_PENDING,
            DeliveryStatus.ARCHIVED,
            DeliveryStatus.FAILED,
        }:
            raise InfrastructureError("task commit must complete before archiving")
        run_dir = run_store.run_dir(task_id)
        commit = self._read_json(run_dir / "delivery" / "commit.json")
        if commit.get("status") != "committed":
            raise InfrastructureError("archive packet has no committed delivery record")
        outbox_path = run_dir / "archive" / "outbox.json"
        if outbox_path.is_file():
            return self.retry(task_id, store=run_store)
        review = (
            run_store.load_latest_review(task_id)
            if task.queue_id is not None
            else run_store.load_review(task_id)
        )
        audit = AuditRecorder(
            run_dir,
            state.repo_root,
            state.base_commit,
            inherited_baseline=state.inherited_baseline,
            queue_task=task.queue_id is not None,
        )
        state.delivery_status = DeliveryStatus.ARCHIVE_PENDING
        run_store.save_state(state)
        packet = self._packet(run_dir, task, state, review.to_dict(), commit)
        archive_dir = run_dir / "archive"
        _atomic_write_json(archive_dir / "context.json", archive_context.to_dict())
        _atomic_write_json(archive_dir / "packet.json", packet)
        if not audit.has_event("archive.queued"):
            audit.append(
                "archive.queued",
                {
                    "commit_sha": commit["commit_sha"],
                    "packet_sha256": _json_sha(packet),
                },
            )
        role_result = self.role_runner.run(
            role="archiver",
            prompt=json.dumps(
                {
                    "archive_packet": packet,
                    "dedupe_context": archive_context.to_dict(),
                    "candidate_rules": {
                        "allowed_types": ["guideline", "pitfall"],
                        "allowed_layers": ["layer1", "layer2", "layer3"],
                        "layer_rules": {
                            "layer1": "cross-project reusable technical knowledge",
                            "layer2": (
                                "cross-project reusable business knowledge with an "
                                "allowlisted business_domain; allowed values are "
                                f"{sorted(self.business_domains)}"
                            ),
                            "layer3": (
                                "knowledge valid only for the current project; project_id "
                                f"is {self.project_id}"
                            ),
                            "forbidden": ["layer0p", "layer0t"],
                            "no_fallback": (
                                "never downgrade an unclassifiable or invalid candidate "
                                "to layer3"
                            ),
                        },
                        "maturity": "draft",
                        "max_candidates": 3,
                        "no_candidate_allowed": True,
                        "knowledge_language": "zh-CN",
                        "knowledge_language_fields": [
                            "task_summary",
                            "title",
                            "scope",
                            "problem",
                            "action",
                            "result",
                            "layer_reason",
                        ],
                        "tag_language": "en",
                        "tag_format": "lowercase ASCII tokens",
                        "proper_nouns": (
                            "technology names and code identifiers may remain unchanged"
                        ),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            output_model=ArchiveRoleOutput,
            artifact_dir=archive_dir / "role",
        )
        output = ArchiveRoleOutput.model_validate(role_result.output.model_dump())
        candidates, candidate_warnings = self._validated_candidates(
            output.candidates, packet, archive_context, task_id, commit["commit_sha"]
        )
        summary = {
            "schema_version": 1,
            "task_id": task_id,
            "project_id": self.project_id,
            "requirement": task.requirement,
            "summary": output.task_summary,
            "tags": list(dict.fromkeys(output.tags)),
            "paths": [str(item.get("path")) for item in packet["changed_files"]],
            "technologies": list(dict.fromkeys(output.technologies)),
            "review_status": state.review_status.value,
            "delivery_status": DeliveryStatus.ARCHIVE_PENDING.value,
            "commit_sha": commit["commit_sha"],
            "committed_at": commit["committed_at"],
            "archive_role_thread_id": role_result.thread_id,
            "candidate_count": len(candidates),
            "candidate_warnings": candidate_warnings,
            "created_at": utc_now_iso(),
        }
        _atomic_write_json(archive_dir / "summary.json", summary)
        self.memory.write_summary(summary)
        outbox = self._build_outbox(
            task_id=task_id,
            commit=commit,
            candidates=candidates,
            knowledge_references=packet["knowledge_references"],
        )
        _atomic_write_json(archive_dir / "outbox.json", outbox)
        try:
            completed = self.retry(task_id, store=run_store)
        except Exception as exc:
            state = run_store.load_state(task_id)
            state.delivery_status = DeliveryStatus.FAILED
            state.last_error_summary = str(exc)
            run_store.save_state(state)
            audit.append(
                "knowledge.write_failed",
                {"error": str(exc), "commit_sha": commit["commit_sha"]},
                redacted=True,
            )
            return self._read_json(archive_dir / "outbox.json")
        if completed["status"] == "completed":
            audit.append(
                "archive.completed",
                {
                    "commit_sha": commit["commit_sha"],
                    "candidate_count": len(candidates),
                },
            )
        return completed

    def retry(
        self, task_id: str, *, store: StateStore | None = None
    ) -> dict[str, Any]:
        run_store = store or StateStore(self.repo_root)
        run_dir = run_store.run_dir(task_id)
        path = run_dir / "archive" / "outbox.json"
        outbox = self._read_json(path)
        state = run_store.load_state(task_id)
        state.delivery_status = DeliveryStatus.ARCHIVE_PENDING
        run_store.save_state(state)
        for item in outbox.get("items", []):
            if item.get("status") == "completed":
                continue
            try:
                result = self.archive_client.call_tool(
                    str(item["tool"]), dict(item["arguments"])
                )
            except McpCallError as exc:
                item["status"] = "failed"
                item["error"] = str(exc)
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["last_attempt_at"] = utc_now_iso()
                outbox["status"] = "failed"
                _atomic_write_json(path, outbox)
                state.delivery_status = DeliveryStatus.FAILED
                state.last_error_summary = str(exc)
                run_store.save_state(state)
                raise
            item["status"] = "completed"
            item["result"] = result
            item["error"] = ""
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["last_attempt_at"] = utc_now_iso()
            _atomic_write_json(path, outbox)
        outbox["status"] = "completed"
        outbox["completed_at"] = utc_now_iso()
        _atomic_write_json(path, outbox)
        state.delivery_status = DeliveryStatus.ARCHIVED
        state.last_error_summary = ""
        run_store.save_state(state)
        summary_path = run_dir / "archive" / "summary.json"
        if summary_path.is_file():
            summary = self._read_json(summary_path)
            summary["delivery_status"] = DeliveryStatus.ARCHIVED.value
            _atomic_write_json(summary_path, summary)
        return outbox

    def _packet(
        self,
        run_dir: Path,
        task: Any,
        state: Any,
        review: Mapping[str, Any],
        commit: Mapping[str, Any],
    ) -> dict[str, Any]:
        changes = self._read_json(run_dir / "changes" / "files.json")
        evaluation = self._optional_json(run_dir / "evaluations" / "aggregate.json")
        failure_summaries = [
            {
                "round_number": item.round_number,
                "passed": item.passed,
                "failure_summary": item.failure_summary,
            }
            for item in state.rounds
        ]
        knowledge_references: list[dict[str, Any]] = []
        seen: set[tuple[str, int, str]] = set()
        for path in (
            run_dir / "context" / "generation.json",
            run_dir / "context" / "evaluation.json",
        ):
            context = self._optional_json(path)
            for item in context.get("knowledge", []):
                key = (
                    str(item.get("knowledge_id", "")),
                    int(item.get("revision", 1)),
                    str(item.get("path", "")),
                )
                if not key[0] or key in seen:
                    continue
                seen.add(key)
                knowledge_references.append(
                    {
                        "knowledge_id": key[0],
                        "revision": key[1],
                        "path": key[2],
                        "used_in": str(item.get("stage", "context")),
                    }
                )
        return redact_sensitive_data(
            {
                "schema_version": 1,
                "source_ids": [
                    f"task:{task.task_id}",
                    f"commit:{commit['commit_sha']}",
                    *[f"validation:{item['round_number']}" for item in failure_summaries],
                ],
                "task_id": task.task_id,
                "queue_id": task.queue_id,
                "requirement": task.requirement,
                "acceptance_criteria": list(task.acceptance_criteria),
                "validation": failure_summaries,
                "changed_files": list(changes.get("files", [])),
                "evaluation": evaluation,
                "review": {
                    "reviewer": review.get("reviewer"),
                    "comment": review.get("comment"),
                    "reviewed_diff_sha256": review.get("reviewed_diff_sha256"),
                },
                "commit": {
                    key: commit.get(key)
                    for key in (
                        "commit_sha",
                        "parent",
                        "tree",
                        "subject",
                        "committed_at",
                    )
                },
                "knowledge_references": knowledge_references,
                "excluded": ["full command logs", "full diff"],
                "created_at": utc_now_iso(),
            }
        )

    def _validated_candidates(
        self,
        candidates: list[ArchiveCandidate],
        packet: Mapping[str, Any],
        context: ContextSnapshot,
        task_id: str,
        commit_sha: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        allowed_sources = set(str(item) for item in packet.get("source_ids", []))
        existing = {
            _normalized_text(" ".join([item.title, item.content]))
            for item in context.knowledge
        }
        values: list[dict[str, Any]] = []
        warnings: list[str] = []
        for candidate in candidates[:3]:
            unknown = set(candidate.source_ids) - allowed_sources
            if unknown:
                raise InfrastructureError(
                    "archiver candidate cited unknown source ids: "
                    + ", ".join(sorted(unknown))
                )
            if (
                candidate.target_layer == "layer2"
                and candidate.business_domain not in self.business_domains
            ):
                warnings.append(
                    f"candidate {candidate.title!r} rejected because business_domain "
                    f"{candidate.business_domain!r} is not allowlisted"
                )
                continue
            content = candidate.content()
            if SENSITIVE_PATTERN.search(" ".join([candidate.title, content])):
                warnings.append(f"candidate {candidate.title!r} rejected as sensitive")
                continue
            normalized = _normalized_text(" ".join([candidate.title, content]))
            if normalized in existing:
                warnings.append(f"candidate {candidate.title!r} skipped as duplicate")
                continue
            candidate_sha = _json_sha(candidate.model_dump(mode="json"))
            idempotency_key = _json_sha(
                {
                    "project_id": self.project_id,
                    "task_id": task_id,
                    "commit_sha": commit_sha,
                    "candidate_sha256": candidate_sha,
                }
            )
            values.append(
                {
                    "title": candidate.title,
                    "knowledge_type": candidate.candidate_type,
                    "content": content,
                    "target_layer": candidate.target_layer,
                    "layer_reason": candidate.layer_reason,
                    "business_domain": candidate.business_domain,
                    "source_references": list(candidate.source_ids),
                    "tags": list(dict.fromkeys(candidate.tags)),
                    "candidate_sha256": candidate_sha,
                    "idempotency_key": idempotency_key,
                }
            )
            existing.add(normalized)
        return values, warnings

    def _build_outbox(
        self,
        *,
        task_id: str,
        commit: Mapping[str, Any],
        candidates: list[Mapping[str, Any]],
        knowledge_references: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            items.append(
                {
                    "idempotency_key": candidate["idempotency_key"],
                    "tool": "knowledge_create_draft",
                    "arguments": {
                        "title": candidate["title"],
                        "knowledge_type": candidate["knowledge_type"],
                        "content": candidate["content"],
                        "target_layer": candidate["target_layer"],
                        "project_id": self.project_id,
                        "business_domain": candidate.get("business_domain") or "",
                        "source_references": candidate["source_references"],
                        "tags": candidate["tags"],
                        "actor": self.writer_actor,
                        "session": f"orchestrator:{task_id}",
                        "idempotency_key": candidate["idempotency_key"],
                    },
                    "status": "pending",
                    "attempts": 0,
                }
            )
        for reference in knowledge_references:
            key = _json_sha(
                {
                    "project_id": self.project_id,
                    "task_id": task_id,
                    "commit_sha": commit["commit_sha"],
                    "knowledge_id": reference["knowledge_id"],
                    "revision": reference["revision"],
                }
            )
            items.append(
                {
                    "idempotency_key": key,
                    "tool": "knowledge_reference",
                    "arguments": {
                        "knowledge_id": reference["knowledge_id"],
                        "project_id": self.project_id,
                        "workflow_id": task_id,
                        "used_in": reference["used_in"],
                        "actor": self.consumer_actor,
                        "session": f"orchestrator:{task_id}",
                    },
                    "status": "pending",
                    "attempts": 0,
                }
            )
        workflow_key = _json_sha(
            {
                "project_id": self.project_id,
                "task_id": task_id,
                "commit_sha": commit["commit_sha"],
                "operation": "workflow_complete",
            }
        )
        items.append(
            {
                "idempotency_key": workflow_key,
                "tool": "knowledge_workflow_complete",
                "arguments": {
                    "actor": self.writer_actor,
                    "session": f"orchestrator:{task_id}:{workflow_key}",
                },
                "status": "pending",
                "attempts": 0,
            }
        )
        return {
            "schema_version": 1,
            "task_id": task_id,
            "commit_sha": commit["commit_sha"],
            "status": "pending",
            "items": items,
            "created_at": utc_now_iso(),
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InfrastructureError(f"archive artifact is unreadable: {path}") from exc
        if not isinstance(value, dict):
            raise InfrastructureError(f"archive artifact must be an object: {path}")
        return value

    @staticmethod
    def _optional_json(path: Path) -> dict[str, Any]:
        return ArchiveCoordinator._read_json(path) if path.is_file() else {}


def _json_sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[\w.+#/-]+", value.casefold()))


__all__ = ["ArchiveCandidate", "ArchiveCoordinator", "ArchiveRoleOutput"]
