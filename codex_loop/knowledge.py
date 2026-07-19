"""Progressive, source-preserving knowledge access through the local MCP."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .mcp_client import DEFAULT_MCP_REGISTRY, LocalMcpClient


STAGE_KNOWLEDGE_TYPES: dict[str, list[str] | None] = {
    "planner": ["decision", "guideline", "process", "model"],
    "generation": None,
    "spec_evaluation": ["guideline", "process", "decision"],
    "architecture_evaluation": ["decision", "model", "guideline", "pitfall"],
    "archive": ["guideline", "pitfall"],
}
STRONG_TYPES = {"decision", "guideline", "process"}


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    knowledge_id: str
    title: str
    path: str
    knowledge_type: str
    layer: str
    scope: str
    owner_id: str | None
    maturity: str
    conflict_status: str
    revision: int
    tags: tuple[str, ...]
    content: str
    content_sha256: str
    selection_reason: str
    stage: str
    match_score: int = 0
    truncated: bool = False
    project_id: str | None = None

    @property
    def constraint_strength(self) -> str:
        if (
            self.maturity in {"verified", "proven"}
            and self.conflict_status not in {"suspected", "confirmed"}
            and self.knowledge_type in STRONG_TYPES
        ):
            return "strong"
        if self.maturity == "draft" or self.knowledge_type == "pitfall":
            return "warning"
        if self.maturity == "legacy" or self.conflict_status != "none":
            return "warning"
        return "context"

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "path": self.path,
            "type": self.knowledge_type,
            "layer": self.layer,
            "scope": self.scope,
            "owner_id": self.owner_id,
            "project_id": self.project_id,
            "maturity": self.maturity,
            "conflict_status": self.conflict_status,
            "revision": self.revision,
            "tags": list(self.tags),
            "content": self.content,
            "content_sha256": self.content_sha256,
            "selection_reason": self.selection_reason,
            "stage": self.stage,
            "match_score": self.match_score,
            "truncated": self.truncated,
            "constraint_strength": self.constraint_strength,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KnowledgeItem":
        return cls(
            knowledge_id=str(data["knowledge_id"]),
            title=str(data.get("title", "")),
            path=str(data["path"]),
            knowledge_type=str(data.get("type", "legacy")),
            layer=str(data.get("layer", "legacy")),
            scope=str(data.get("scope", "team")),
            owner_id=(
                None if data.get("owner_id") is None else str(data["owner_id"])
            ),
            maturity=str(data.get("maturity", "legacy")),
            conflict_status=str(data.get("conflict_status", "none")),
            revision=int(data.get("revision", 1)),
            tags=tuple(str(item) for item in data.get("tags", [])),
            content=str(data.get("content", "")),
            content_sha256=str(data.get("content_sha256", "")),
            selection_reason=str(data.get("selection_reason", "")),
            stage=str(data.get("stage", "")),
            match_score=int(data.get("match_score", 0)),
            truncated=bool(data.get("truncated", False)),
            project_id=(
                None if data.get("project_id") is None else str(data["project_id"])
            ),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeSelection:
    stage: str
    query: str
    actor: str
    catalog_sha256: str
    items: tuple[KnowledgeItem, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "query": self.query,
            "actor": self.actor,
            "catalog_sha256": self.catalog_sha256,
            "items": [item.to_dict() for item in self.items],
            "warnings": list(self.warnings),
        }


class KnowledgeGateway:
    """Perform the explicit Layer A -> search -> selected entry progression."""

    def __init__(
        self,
        *,
        project_id: str,
        registry_path: str | Path = DEFAULT_MCP_REGISTRY,
        client: LocalMcpClient | None = None,
    ) -> None:
        if not str(project_id).strip():
            raise ValueError("knowledge gateway project_id must not be blank")
        self.project_id = str(project_id)
        self.registry_path = Path(registry_path).expanduser().resolve()
        self.client = client or LocalMcpClient(self.registry_path, mode="read")
        self.registry = json.loads(self.registry_path.read_text(encoding="utf-8"))

    def budget(self, stage: str) -> dict[str, int]:
        values = dict(self.registry.get("budgets", {})).get(stage, {})
        return {
            "max_catalogs": max(1, int(values.get("max_catalogs", 3))),
            "max_entries": max(1, int(values.get("max_entries", 8))),
            "max_chars": max(1000, int(values.get("max_chars", 24000))),
        }

    def aliases(self) -> dict[str, list[str]]:
        raw = self.registry.get("aliases", {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): [str(item) for item in values]
            for key, values in raw.items()
            if isinstance(values, list)
        }

    def retrieve(
        self,
        *,
        stage: str,
        query: str,
        actor: str = "",
        changed_paths: list[str] | None = None,
    ) -> KnowledgeSelection:
        if stage not in STAGE_KNOWLEDGE_TYPES:
            raise ValueError(f"unsupported knowledge stage: {stage}")
        budget = self.budget(stage)
        catalog = self.client.call_tool(
            "knowledge_catalog",
            {"max_chars": min(20000, budget["max_chars"] // 3)},
        )
        effective_query = " ".join(
            part for part in [str(query).strip(), " ".join(changed_paths or [])] if part
        )
        searched = self.client.call_tool(
            "knowledge_search",
            {
                "query": effective_query,
                "actor": str(actor),
                "project_id": self.project_id,
                "knowledge_types": STAGE_KNOWLEDGE_TYPES[stage],
                "max_results": budget["max_entries"],
            },
        )
        items: list[KnowledgeItem] = []
        warnings: list[str] = []
        remaining = budget["max_chars"]
        for result in searched.get("results", []):
            if remaining <= 0:
                warnings.append("knowledge character budget exhausted")
                break
            read = self.client.call_tool(
                "knowledge_read",
                {
                    "path": str(result["path"]),
                    "actor": str(actor),
                    "project_id": self.project_id,
                    "max_chars": remaining,
                },
            )
            metadata = read.get("metadata", {})
            content = str(read.get("content", ""))
            remaining -= len(content)
            matched_terms = [str(item) for item in result.get("matched_terms", [])]
            item = KnowledgeItem(
                knowledge_id=str(result["knowledge_id"]),
                title=str(result.get("title", metadata.get("title", ""))),
                path=str(result["path"]),
                knowledge_type=str(result.get("type", metadata.get("type", "legacy"))),
                layer=str(result.get("layer", metadata.get("layer", "legacy"))),
                scope=str(result.get("scope", metadata.get("scope", "team"))),
                owner_id=(
                    None
                    if result.get("owner_id", metadata.get("owner_id")) is None
                    else str(result.get("owner_id", metadata.get("owner_id")))
                ),
                project_id=(
                    None
                    if result.get("project_id", metadata.get("project_id")) is None
                    else str(result.get("project_id", metadata.get("project_id")))
                ),
                maturity=str(result.get("maturity", metadata.get("maturity", "legacy"))),
                conflict_status=str(
                    result.get(
                        "conflict_status", metadata.get("conflict_status", "none")
                    )
                ),
                revision=int(result.get("revision", metadata.get("revision", 1))),
                tags=tuple(str(value) for value in result.get("tags", metadata.get("tags", []))),
                content=content,
                content_sha256=str(read["content_sha256"]),
                selection_reason=(
                    "lexical match: " + ", ".join(matched_terms)
                    if matched_terms
                    else "stage catalog selection"
                ),
                stage=stage,
                match_score=int(result.get("match_score", 0)),
                truncated=bool(read.get("truncated", False)),
            )
            items.append(item)
            if item.constraint_strength == "warning":
                warnings.append(
                    f"{item.knowledge_id} is advisory only "
                    f"({item.maturity}/{item.conflict_status}/{item.knowledge_type})"
                )
            if item.truncated:
                warnings.append(f"{item.knowledge_id} was truncated by budget")
        return KnowledgeSelection(
            stage=stage,
            query=effective_query,
            actor=str(actor),
            catalog_sha256=str(catalog["content_sha256"]),
            items=tuple(items),
            warnings=tuple(dict.fromkeys(warnings)),
        )


def knowledge_snapshot_sha256(items: list[KnowledgeItem] | tuple[KnowledgeItem, ...]) -> str:
    payload = [
        {
            "knowledge_id": item.knowledge_id,
            "revision": item.revision,
            "path": item.path,
            "project_id": item.project_id,
            "content_sha256": item.content_sha256,
            "stage": item.stage,
        }
        for item in items
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "KnowledgeGateway",
    "KnowledgeItem",
    "KnowledgeSelection",
    "STAGE_KNOWLEDGE_TYPES",
    "knowledge_snapshot_sha256",
]
