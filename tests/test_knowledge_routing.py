from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from codex_loop.context import ContextAssembler, ContextSnapshot
from codex_loop.knowledge import KnowledgeGateway
from codex_loop.models import InfrastructureError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeMcp:
    mode = "read"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.layer_a = (
            "# map\n"
            "- Layer 1: `tech-wiki/catalog.md`\n"
            "- Layer 2: `biz-wiki/finance/catalog.md`\n"
            "- Layer 3: `docs/knowledge/catalog.md`\n"
        )
        self.layer_b = {
            "tech-wiki/catalog.md": (
                "| ID | 标题 | 路径 |\n"
                "| `TK-GDL-013` | CLI persistence | "
                "`tech-wiki/guidelines/TK-GDL-013.md` |\n"
            ),
            "biz-wiki/finance/catalog.md": "| ID | 标题 |\n| - | - |\n",
            "docs/knowledge/catalog.md": "| ID | 标题 |\n| - | - |\n",
        }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if name == "knowledge_catalog":
            path = str(arguments.get("path", "knowledge-catalog.md"))
            content = self.layer_a if path == "knowledge-catalog.md" else self.layer_b[path]
            return {
                "path": path,
                "content": content,
                "content_sha256": digest(content),
                "layer": "A" if path == "knowledge-catalog.md" else "B",
            }
        if name == "knowledge_search":
            assert arguments["catalog_paths"] == ["tech-wiki/catalog.md"]
            return {
                "query": arguments["query"],
                "results": [
                    {
                        "knowledge_id": "TK-GDL-013",
                        "title": "为命令行持久化功能同时验证写入、读取和空数据状态",
                        "path": "tech-wiki/guidelines/TK-GDL-013.md",
                        "type": "guideline",
                        "layer": "layer1",
                        "scope": "team",
                        "maturity": "verified",
                        "conflict_status": "none",
                        "revision": 1,
                        "tags": ["cli", "persistence"],
                        "matched_terms": ["cli", "persistence"],
                        "match_score": 8,
                    }
                ],
            }
        if name == "knowledge_read":
            content = "验证 CLI 的写入、读取和空数据状态。"
            return {
                "path": arguments["path"],
                "metadata": {},
                "content": content,
                "content_sha256": digest(content),
                "truncated": False,
            }
        raise AssertionError(f"unexpected MCP tool: {name}")


def make_gateway(tmp_path: Path, client: FakeMcp) -> KnowledgeGateway:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "budgets": {
                    "generation": {
                        "max_catalogs": 1,
                        "max_entries": 3,
                        "max_chars": 4000,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return KnowledgeGateway(
        project_id="reading-notes",
        registry_path=registry,
        client=client,
    )


def test_retrieve_uses_layer_a_then_selected_layer_b_before_layer_c(
    tmp_path: Path,
) -> None:
    client = FakeMcp()
    selection = make_gateway(tmp_path, client).retrieve(
        stage="generation",
        query="python cli persistence",
        actor="local-user",
    )

    assert selection.layer_a_catalog is not None
    assert selection.layer_a_catalog.path == "knowledge-catalog.md"
    assert [item.path for item in selection.layer_b_catalogs] == [
        "tech-wiki/catalog.md"
    ]
    assert selection.retrieval_route[0]["from"] == "layer_a"
    assert selection.retrieval_route[0]["to"] == "layer_b"
    assert selection.items[0].selection_reason.startswith(
        "Layer B=tech-wiki/catalog.md"
    )

    names = [name for name, _arguments in client.calls]
    assert names == [
        "knowledge_catalog",
        "knowledge_catalog",
        "knowledge_search",
        "knowledge_read",
    ]


def test_context_snapshot_persists_and_hashes_retrieval_route(tmp_path: Path) -> None:
    client = FakeMcp()
    gateway = make_gateway(tmp_path, client)

    class NoSkills:
        def select(self, **_kwargs: Any) -> tuple[list[Any], list[str]]:
            return [], []

    class NoMemory:
        def recall(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

    path = tmp_path / "generation.json"
    snapshot = ContextAssembler(gateway, NoSkills(), NoMemory()).assemble(
        path=path,
        stage="generation",
        query="python cli persistence",
        actor="local-user",
    )
    restored = ContextSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))

    assert restored.layer_a_catalog == snapshot.layer_a_catalog
    assert restored.layer_b_catalogs == snapshot.layer_b_catalogs
    assert restored.retrieval_route == snapshot.retrieval_route
    restored.verify_hash()


def test_context_snapshot_has_no_version_field_and_rejects_old_payload() -> None:
    snapshot = ContextSnapshot(stage="generation", query="query", actor="actor")
    payload = snapshot.to_dict()
    assert "schema_version" not in payload
    with pytest.raises(InfrastructureError, match="schema_version is no longer supported"):
        ContextSnapshot.from_dict({**payload, "schema_version": 1})


def test_layer_a_without_layer_b_catalog_fails_closed(tmp_path: Path) -> None:
    client = FakeMcp()
    client.layer_a = "# map without catalog routes\n"
    with pytest.raises(RuntimeError, match="no Layer B catalogs"):
        make_gateway(tmp_path, client).retrieve(
            stage="generation",
            query="python",
            actor="local-user",
        )
