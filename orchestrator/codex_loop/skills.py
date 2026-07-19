"""Selection of plain-text, advisory Skills from the local MCP registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

from .mcp_client import LocalMcpClient, McpCallError


@dataclass(frozen=True, slots=True)
class SkillItem:
    skill_id: str
    version: str
    stages: tuple[str, ...]
    triggers: tuple[str, ...]
    path: str
    content: str
    content_sha256: str
    selection_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "stages": list(self.stages),
            "triggers": list(self.triggers),
            "path": self.path,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "selection_reason": self.selection_reason,
            "constraint_strength": "advisory",
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SkillItem":
        return cls(
            skill_id=str(data["skill_id"]),
            version=str(data["version"]),
            stages=tuple(str(item) for item in data.get("stages", [])),
            triggers=tuple(str(item) for item in data.get("triggers", [])),
            path=str(data["path"]),
            content=str(data.get("content", "")),
            content_sha256=str(data.get("content_sha256", "")),
            selection_reason=str(data.get("selection_reason", "")),
        )


class SkillRegistry:
    """Select Skills by stage and unique local trigger matches."""

    def __init__(self, client: LocalMcpClient) -> None:
        if client.mode != "read":
            raise ValueError("SkillRegistry requires a read-mode MCP client")
        self.client = client

    def select(
        self,
        *,
        stage: str,
        query: str,
        changed_paths: list[str] | None = None,
        max_skills: int = 4,
    ) -> tuple[list[SkillItem], list[str]]:
        haystack = " ".join([query, *(changed_paths or [])]).casefold()
        tokens = set(re.findall(r"[\w.+#/-]+", haystack))
        try:
            listed = self.client.call_tool("skill_list", {"stage": stage})
        except McpCallError as exc:
            return [], [f"Skill capability degraded: {exc}"]
        ranked: list[tuple[int, str, Mapping[str, Any]]] = []
        for raw in listed.get("skills", []):
            triggers = {str(item).casefold() for item in raw.get("triggers", [])}
            path_suffix = Path(str(raw.get("path", ""))).suffix.casefold().lstrip(".")
            matched = triggers & tokens
            if not matched and path_suffix and path_suffix in tokens:
                matched.add(path_suffix)
            if not matched and triggers:
                continue
            ranked.append((len(matched), str(raw.get("skill_id", "")), raw))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected: list[SkillItem] = []
        warnings: list[str] = []
        for score, skill_id, raw in ranked[: max(0, int(max_skills))]:
            try:
                read = self.client.call_tool(
                    "skill_read", {"skill_id": skill_id, "max_chars": 20000}
                )
            except McpCallError as exc:
                warnings.append(f"Skill {skill_id} unavailable: {exc}")
                continue
            selected.append(
                SkillItem(
                    skill_id=skill_id,
                    version=str(read["version"]),
                    stages=tuple(str(item) for item in read.get("stages", [])),
                    triggers=tuple(str(item) for item in read.get("triggers", [])),
                    path=str(read["path"]),
                    content=str(read["content"]),
                    content_sha256=str(read["content_sha256"]),
                    selection_reason=f"stage={stage}; unique trigger matches={score}",
                )
            )
            if bool(read.get("truncated", False)):
                warnings.append(f"Skill {skill_id} was truncated")
        return selected, warnings


__all__ = ["SkillItem", "SkillRegistry"]
