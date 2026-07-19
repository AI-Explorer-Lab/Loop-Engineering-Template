"""Official-SDK client for the capability-limited local stdio MCP server."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .models import InfrastructureError
from .state import redact_sensitive_data, redact_sensitive_text


DEFAULT_MCP_REGISTRY = Path(
    os.environ.get("ORCHESTRATOR_MCP_REGISTRY", "mcp/registry.json")
)
MODE_TOOLS = {
    "read": {
        "knowledge_catalog",
        "knowledge_search",
        "knowledge_read",
        "skill_list",
        "skill_read",
        "resource_read",
    },
    "archive": {
        "knowledge_create_draft",
        "knowledge_reference",
        "knowledge_workflow_complete",
    },
}

EventSink = Callable[[str, Mapping[str, Any]], None]


class McpCallError(InfrastructureError):
    """A local MCP handshake, capability, or tool failure."""


class LocalMcpClient:
    """Launch a fresh offline stdio server for each bounded control-plane call."""

    def __init__(
        self,
        registry_path: str | Path = DEFAULT_MCP_REGISTRY,
        *,
        mode: str = "read",
        timeout_seconds: float = 30.0,
        event_sink: EventSink | None = None,
        python_executable: str | Path | None = None,
    ) -> None:
        if mode not in MODE_TOOLS:
            raise ValueError("mode must be read or archive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.registry_path = Path(registry_path).expanduser().resolve()
        self.mode = mode
        self.timeout_seconds = float(timeout_seconds)
        self.event_sink = event_sink
        self.python_executable = Path(python_executable or sys.executable).resolve()
        self._registry = self._load_registry()
        self.server_path = self._server_path()

    def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call one mode-allowed tool and return its structured JSON object."""

        normalized_name = str(name).strip()
        if normalized_name not in MODE_TOOLS[self.mode]:
            raise McpCallError(
                f"MCP tool {normalized_name!r} is unavailable in {self.mode} mode"
            )
        safe_arguments = dict(arguments or {})
        try:
            result = anyio.run(self._call_tool, normalized_name, safe_arguments)
        except McpCallError:
            raise
        except TimeoutError as exc:
            raise McpCallError("Local MCP tool call timed out") from exc
        except Exception as exc:
            detail = _exception_detail(exc)
            raise McpCallError(f"Local MCP tool call failed: {detail}") from exc
        self._emit(
            "mcp.tool_completed",
            {
                "mode": self.mode,
                "tool": normalized_name,
                "argument_keys": sorted(safe_arguments),
                "result_sha256": _json_sha256(result),
            },
        )
        return result

    def health(self) -> dict[str, Any]:
        """Verify handshake and report only tools allowed for this mode."""

        try:
            return anyio.run(self._health)
        except Exception as exc:
            detail = _exception_detail(exc)
            raise McpCallError(f"Local MCP health check failed: {detail}") from exc

    async def _call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        with anyio.fail_after(self.timeout_seconds):
            async with stdio_client(self._server_parameters()) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                ) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    available = {tool.name for tool in listed.tools}
                    if available != MODE_TOOLS[self.mode]:
                        raise McpCallError(
                            "Local MCP exposed an unexpected capability set"
                        )
                    response = await session.call_tool(name, arguments)
                    if bool(getattr(response, "isError", False)):
                        raise McpCallError(_tool_error(response))
                    value = _structured_result(response)
                    if not isinstance(value, dict):
                        raise McpCallError("Local MCP returned a non-object result")
                    return value

    async def _health(self) -> dict[str, Any]:
        with anyio.fail_after(self.timeout_seconds):
            async with stdio_client(self._server_parameters()) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
                ) as session:
                    initialization = await session.initialize()
                    listed = await session.list_tools()
                    tools = sorted(tool.name for tool in listed.tools)
                    if set(tools) != MODE_TOOLS[self.mode]:
                        raise McpCallError(
                            "Local MCP exposed an unexpected capability set"
                        )
                    server_info = getattr(initialization, "serverInfo", None)
                    return {
                        "status": "healthy",
                        "mode": self.mode,
                        "transport": "stdio",
                        "network": "disabled",
                        "server_name": str(getattr(server_info, "name", "harness-local")),
                        "server_version": str(getattr(server_info, "version", "unknown")),
                        "tools": tools,
                    }

    def _server_parameters(self) -> StdioServerParameters:
        environment: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
        for name in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return StdioServerParameters(
            command=str(self.python_executable),
            args=[
                str(self.server_path),
                "--registry",
                str(self.registry_path),
                "--mode",
                self.mode,
            ],
            env=environment,
            cwd=str(self.registry_path.parent),
        )

    def _load_registry(self) -> dict[str, Any]:
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise McpCallError("Local MCP registry is unreadable") from exc
        if not isinstance(value, dict):
            raise McpCallError("Local MCP registry must contain one object")
        server = value.get("server")
        if not isinstance(server, dict):
            raise McpCallError("Local MCP registry has no server object")
        if server.get("transport") != "stdio" or server.get("network") != "disabled":
            raise McpCallError("Local MCP must use offline stdio mode")
        return value

    def _server_path(self) -> Path:
        entrypoint = str(dict(self._registry["server"]).get("entrypoint", ""))
        raw = Path(entrypoint)
        if raw.is_absolute() or ".." in raw.parts or not raw.parts:
            raise McpCallError("Local MCP entrypoint is unsafe")
        path = (self.registry_path.parent / raw).resolve()
        try:
            path.relative_to(self.registry_path.parent)
        except ValueError as exc:
            raise McpCallError("Local MCP entrypoint escapes its root") from exc
        if not path.is_file() or path.is_symlink():
            raise McpCallError("Local MCP entrypoint does not exist")
        return path

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, redact_sensitive_data(dict(payload)))


def _structured_result(response: Any) -> Any:
    structured = getattr(response, "structuredContent", None)
    if structured is not None:
        return structured
    structured = getattr(response, "structured_content", None)
    if structured is not None:
        return structured
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    raise McpCallError("Local MCP returned no structured content")


def _tool_error(response: Any) -> str:
    messages = [
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", []) or []
        if getattr(block, "text", None)
    ]
    return redact_sensitive_text("; ".join(messages) or "Local MCP tool failed")[:2_000]


def _exception_detail(error: BaseException) -> str:
    """Flatten task-group wrappers while keeping only redacted local error text."""

    children = getattr(error, "exceptions", None)
    if isinstance(children, tuple) and children:
        details = [_exception_detail(child) for child in children]
        return " | ".join(dict.fromkeys(item for item in details if item))[:2_000]
    return redact_sensitive_text(str(error) or type(error).__name__)[:2_000]


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        redact_sensitive_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DEFAULT_MCP_REGISTRY",
    "LocalMcpClient",
    "MODE_TOOLS",
    "McpCallError",
]
