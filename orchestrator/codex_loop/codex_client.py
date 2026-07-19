"""Small, version-isolated adapter around the Codex Python SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
import os
from pathlib import Path
import platform
import re
import subprocess
from types import TracebackType
from typing import Any, Callable, Mapping, Self

from .models import InfrastructureError
from .state import redact_sensitive_text


DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED_CODEX_RUNTIME_VERSION = "0.144.4"
DEFAULT_CODEX_RUNTIME_PATH = (
    DEFAULT_REPO_ROOT / "orchestrator" / "node_modules" / ".bin" / "codex"
)


@dataclass(frozen=True, slots=True)
class CodexRunResult:
    """Stable result shape exposed to the workflow instead of an SDK object."""

    thread_id: str
    final_response: str | None
    turn_id: str | None = None
    usage: dict[str, Any] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    history_complete: bool = True


@dataclass(frozen=True, slots=True)
class _SdkBindings:
    Codex: Any
    CodexConfig: Any
    ApprovalMode: Any
    Sandbox: Any
    TurnStatus: Any
    ConfigReadResponse: Any


class CodexClient:
    """Own one local Codex SDK session and its currently selected thread.

    Imports of ``openai_codex`` are intentionally delayed until the client is
    used. This keeps the rest of the orchestrator importable for reports and
    tests even when the optional SDK is not installed.
    """

    def __init__(
        self,
        repo_root: str | Path | None = None,
        *,
        runtime_path: str | Path | None = None,
        policy: Any | None = None,
        event_sink: Callable[[Any], None] | None = None,
        permission_denial_sink: Callable[[str, Mapping[str, Any]], None]
        | None = None,
        read_only: bool = False,
        base_instructions: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or DEFAULT_REPO_ROOT).expanduser().resolve()
        self.runtime_path = Path(
            runtime_path or DEFAULT_CODEX_RUNTIME_PATH
        ).expanduser().resolve()
        self.policy = policy
        self.event_sink = event_sink
        self.permission_denial_sink = permission_denial_sink
        self.read_only = bool(read_only)
        self.base_instructions = (
            None if base_instructions is None else str(base_instructions)
        )
        self._sdk: _SdkBindings | None = None
        self._codex_context: Any | None = None
        self._codex: Any | None = None
        self._thread: Any | None = None
        self._thread_id: str | None = None
        self._preflight_complete = False
        self._runtime_verified = False

    @property
    def thread_id(self) -> str | None:
        """ID of the active thread, if one has been started or resumed."""

        return self._thread_id

    def __enter__(self) -> Self:
        self.preflight()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            self._shutdown(exc_type, exc, traceback)
        except InfrastructureError:
            # Do not hide the workflow error that caused the context to exit.
            if exc is None:
                raise
        return False

    def preflight(self) -> None:
        """Start the local App Server transport and confirm authentication."""

        if self._preflight_complete:
            return

        codex = self._ensure_codex()
        try:
            account_response = codex.account()
        except Exception as exc:
            try:
                if self.policy is not None:
                    self.policy.retire_app_server_material()
            finally:
                self._discard_codex()
            raise self._error("Codex authentication preflight failed", exc) from exc

        if getattr(account_response, "account", None) is None:
            try:
                if self.policy is not None:
                    self.policy.retire_app_server_material()
            finally:
                self._discard_codex()
            raise InfrastructureError(
                "Codex is not authenticated; sign in before starting the orchestrator"
            )

        if self.policy is not None:
            self.policy.retire_app_server_material()
        self._preflight_complete = True

    def start_thread(self) -> str:
        """Create a persistent thread rooted at this repository."""

        self.preflight()
        assert self._codex is not None
        assert self._sdk is not None

        try:
            arguments: dict[str, Any] = {
                "cwd": str(self.repo_root),
                "approval_mode": self._sdk.ApprovalMode.deny_all,
            }
            if self.read_only:
                arguments["sandbox"] = self._sdk.Sandbox.read_only
            if self.base_instructions:
                arguments["base_instructions"] = self.base_instructions
            thread = self._codex.thread_start(**arguments)
        except Exception as exc:
            raise self._error("Unable to start a Codex thread", exc) from exc

        return self._select_thread(thread, "started")

    def resume_thread(self, thread_id: str) -> str:
        """Resume a stored thread and make it active for subsequent turns."""

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string")

        self.preflight()
        assert self._codex is not None
        assert self._sdk is not None

        try:
            arguments = {
                "cwd": str(self.repo_root),
                "approval_mode": self._sdk.ApprovalMode.deny_all,
            }
            if self.read_only:
                arguments["sandbox"] = self._sdk.Sandbox.read_only
            if self.base_instructions:
                arguments["base_instructions"] = self.base_instructions
            thread = self._codex.thread_resume(thread_id, **arguments)
        except Exception as exc:
            raise self._error("Unable to resume the Codex thread", exc) from exc

        return self._select_thread(thread, "resumed")

    def run(
        self,
        prompt: str,
        *,
        output_schema: Mapping[str, Any] | None = None,
    ) -> CodexRunResult:
        """Run one turn on the active thread and require normal completion."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if self._thread is None or self._thread_id is None:
            raise RuntimeError("start or resume a Codex thread before running a turn")

        assert self._sdk is not None
        try:
            arguments: dict[str, Any] = {
                "approval_mode": self._sdk.ApprovalMode.deny_all,
                "cwd": str(self.repo_root),
            }
            if self.read_only:
                arguments["sandbox"] = self._sdk.Sandbox.read_only
            if output_schema is not None:
                arguments["output_schema"] = dict(output_schema)
            handle = self._thread.turn(prompt, **arguments)
            completed_turn: Any | None = None
            usage: dict[str, Any] | None = None
            visible_response: str | None = None
            completed_items: list[dict[str, Any]] = []
            for notification in handle.stream():
                if self.event_sink is not None:
                    self.event_sink(notification)
                method = getattr(notification, "method", "")
                payload = getattr(notification, "payload", None)
                if method == "item/completed":
                    response = _agent_message_from_payload(payload)
                    if response:
                        visible_response = response
                    item = _completed_item_from_payload(payload)
                    if item is not None:
                        completed_items.append(item)
                if method == "thread/tokenUsage/updated":
                    usage_value = getattr(payload, "token_usage", None)
                    usage = _model_dump(usage_value)
                if method == "turn/completed":
                    completed_turn = getattr(payload, "turn", None)
        except Exception as exc:
            raise self._error("Codex turn could not be completed", exc) from exc

        if completed_turn is None:
            raise InfrastructureError("Codex stream ended without turn/completed")
        status = getattr(completed_turn, "status", None)
        if status != self._sdk.TurnStatus.completed:
            status_name = getattr(status, "value", status)
            turn_error = getattr(completed_turn, "error", None)
            error_name = type(turn_error).__name__ if turn_error is not None else "none"
            error_detail = _turn_error_detail(turn_error)
            detail_suffix = f", detail={error_detail}" if error_detail else ""
            raise InfrastructureError(
                "Codex turn did not complete "
                f"(status={status_name!s}, error_type={error_name}{detail_suffix})"
            )

        for wrapped in getattr(completed_turn, "items", []) or []:
            item = _item_dict(wrapped)
            if item is not None and not _contains_item(completed_items, item):
                completed_items.append(item)

        return CodexRunResult(
            thread_id=self._thread_id,
            turn_id=str(getattr(completed_turn, "id", "")) or None,
            final_response=_final_agent_response(completed_turn) or visible_response,
            usage=usage,
            items=completed_items,
        )

    def effective_config(self) -> dict[str, Any]:
        """Read the App Server's resolved process configuration."""

        self.preflight()
        assert self._codex is not None
        assert self._sdk is not None
        raw_client = getattr(self._codex, "_client", None)
        request = getattr(raw_client, "request", None)
        if not callable(request):
            raise InfrastructureError(
                "Pinned Codex SDK cannot expose effective configuration"
            )
        try:
            response = request(
                "config/read",
                {"cwd": str(self.repo_root), "includeLayers": True},
                response_model=self._sdk.ConfigReadResponse,
            )
        except Exception as exc:
            raise self._error("Unable to read effective Codex configuration", exc) from exc
        config = getattr(response, "config", None)
        values = _model_dump(config)
        if not isinstance(values, dict):
            raise InfrastructureError("Codex returned no effective configuration")
        return values

    def verify_thread_workspace(self) -> None:
        """Confirm App Server persisted the expected absolute task cwd."""

        if self._thread is None:
            raise RuntimeError("start or resume a Codex thread before verifying it")
        try:
            response = self._thread.read(include_turns=False)
            thread_record = getattr(response, "thread", None)
            raw_cwd = getattr(thread_record, "cwd", None)
            actual_cwd = getattr(raw_cwd, "root", raw_cwd)
        except Exception as exc:
            raise self._error("Unable to verify Codex thread workspace", exc) from exc
        if Path(str(actual_cwd)).expanduser().resolve() != self.repo_root:
            raise InfrastructureError("Codex thread is rooted outside the task worktree")

    def verify_turn_completed(
        self, expected_turn_count: int
    ) -> CodexRunResult | None:
        """Prove an interrupted-process checkpoint has a completed SDK turn.

        The workflow owns a dedicated thread, so its durable turn count must
        exactly match the thread history before validation may resume.
        """

        if expected_turn_count < 1:
            raise ValueError("expected_turn_count must be at least 1")
        if self._thread is None:
            raise RuntimeError("resume the Codex thread before inspecting it")

        try:
            response = self._thread.read(include_turns=True)
            thread_record = getattr(response, "thread", None)
            turns = getattr(thread_record, "turns", None)
        except Exception as exc:
            raise self._error("Unable to read Codex turn history", exc) from exc

        if not isinstance(turns, list):
            raise InfrastructureError("Codex returned no readable turn history")
        if len(turns) == expected_turn_count - 1:
            # The checkpoint was persisted after saving the prompt but before
            # App Server accepted the turn. The workflow may safely dispatch
            # the already-saved prompt once.
            return None
        if len(turns) != expected_turn_count:
            raise InfrastructureError(
                "Saved Codex turn was not confirmed "
                f"(expected_turns={expected_turn_count}, actual_turns={len(turns)})"
            )

        assert self._sdk is not None
        last_status = getattr(turns[-1], "status", None)
        if last_status != self._sdk.TurnStatus.completed:
            status_name = getattr(last_status, "value", last_status)
            raise InfrastructureError(
                "Saved Codex turn is not completed "
                f"(status={status_name!s})"
            )
        last_turn = turns[-1]
        history_items = [
            item
            for wrapped in (getattr(last_turn, "items", None) or [])
            if (item := _item_dict(wrapped)) is not None
        ]
        items_view = getattr(last_turn, "items_view", None)
        items_view_value = getattr(items_view, "value", items_view)
        return CodexRunResult(
            thread_id=self._thread_id or "",
            turn_id=str(getattr(last_turn, "id", "")) or None,
            final_response=_final_agent_response(last_turn),
            usage=None,
            items=history_items,
            history_complete=items_view_value in {None, "full"},
        )

    def close(self) -> None:
        """Close the SDK context and its local App Server transport."""

        self._shutdown(None, None, None)

    def _load_sdk(self) -> _SdkBindings:
        if self._sdk is not None:
            return self._sdk

        try:
            sdk_module = import_module("openai_codex")
            types_module = import_module("openai_codex.types")
            generated_module = import_module("openai_codex.generated.v2_all")
            bindings = _SdkBindings(
                Codex=sdk_module.Codex,
                CodexConfig=sdk_module.CodexConfig,
                ApprovalMode=sdk_module.ApprovalMode,
                Sandbox=sdk_module.Sandbox,
                TurnStatus=types_module.TurnStatus,
                ConfigReadResponse=generated_module.ConfigReadResponse,
            )
        except (ImportError, ModuleNotFoundError, AttributeError) as exc:
            raise InfrastructureError(
                "The Codex Python SDK is unavailable; install the pinned "
                "orchestrator requirements"
            ) from exc

        self._sdk = bindings
        return bindings

    def _ensure_codex(self) -> Any:
        if self._codex is not None:
            return self._codex

        sdk = self._load_sdk()
        self._verify_runtime()
        context: Any | None = None
        try:
            config_values: dict[str, Any] = {
                "codex_bin": str(self.runtime_path),
                "experimental_api": False,
            }
            if self.policy is not None:
                # The Python SDK merges ``CodexConfig.env`` into a copy of the
                # parent environment. That is not an allowlist: credentials
                # and outer Codex sandbox markers would otherwise survive.
                # Launch through ``env -i`` so the App Server receives only
                # the task policy's explicitly allowed variables.
                config_values["launch_args_override"] = (
                    self._isolated_app_server_command()
                )
                config_values["cwd"] = str(self.repo_root)
            elif self.read_only:
                config_values["config_overrides"] = (
                    'approval_policy="never"',
                    'web_search="disabled"',
                    "features.apps=false",
                    "features.multi_agent=false",
                    "features.remote_plugin=false",
                    "features.memories=false",
                    "features.skill_mcp_dependency_install=false",
                    "mcp_servers={}",
                    "apps._default.enabled=false",
                    "apps._default.destructive_enabled=false",
                    "apps._default.open_world_enabled=false",
                )
            config = sdk.CodexConfig(**config_values)
            context = sdk.Codex(config)
            codex = context.__enter__()
            raw_client = getattr(codex, "_client", None)
            if raw_client is None or not hasattr(raw_client, "_approval_handler"):
                raise InfrastructureError(
                    "Pinned Codex SDK cannot enforce a deny-all approval handler"
                )
            raw_client._approval_handler = self._deny_approval_request
        except Exception as exc:
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except Exception:
                    pass
            raise self._error("Unable to start the local Codex App Server", exc) from exc

        self._codex_context = context
        self._codex = codex
        return codex

    def _isolated_app_server_command(self) -> tuple[str, ...]:
        """Build an App Server command with a genuinely empty base environment."""

        if self.policy is None:
            raise InfrastructureError("An execution policy is required")
        env_executable = Path("/usr/bin/env")
        if not env_executable.is_file() or not os.access(env_executable, os.X_OK):
            raise InfrastructureError("The system env executable is unavailable")

        environment = self.policy.environment()
        assignments: list[str] = []
        for name, value in sorted(environment.items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
                raise InfrastructureError("Task environment contains an invalid name")
            if "\x00" in str(value):
                raise InfrastructureError("Task environment contains an invalid value")
            assignments.append(f"{name}={value}")

        native_runtime = _native_codex_runtime(self.runtime_path)
        staged_runtime = self.policy.stage_app_server_executable(native_runtime)
        command = [
            *self.policy.app_server_command_prefix(),
            str(env_executable),
            "-i",
            *assignments,
            str(staged_runtime),
        ]
        for override in self.policy.config_overrides():
            command.extend(("--config", str(override)))
        command.extend(("app-server", "--listen", "stdio://"))
        return tuple(command)

    def _deny_approval_request(
        self, method: str, params: Mapping[str, Any] | None
    ) -> dict[str, str]:
        """Deny and audit any unexpected App Server approval request."""

        values = dict(params or {})
        if self.permission_denial_sink is not None:
            self.permission_denial_sink(str(method), values)
        return {"decision": "decline"}

    def _verify_runtime(self) -> None:
        if self._runtime_verified:
            return

        install_command = "npm ci --prefix orchestrator"
        if not self.runtime_path.is_file():
            raise InfrastructureError(
                "Project-local Codex runtime is missing at "
                f"{self.runtime_path}; run `{install_command}` from the repository root"
            )
        if not os.access(self.runtime_path, os.X_OK):
            raise InfrastructureError(
                "Project-local Codex runtime is not executable at "
                f"{self.runtime_path}; run `{install_command}` to reinstall it"
            )

        try:
            completed = subprocess.run(
                [str(self.runtime_path), "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError(
                "Project-local Codex runtime version check timed out"
            ) from exc
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise self._error(
                "Unable to execute the project-local Codex runtime", exc
            ) from exc

        output = "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        ).strip()
        if completed.returncode != 0:
            detail = redact_sensitive_text(output)[:2_000]
            suffix = f": {detail}" if detail else ""
            raise InfrastructureError(
                "Project-local Codex runtime version check failed "
                f"(exit_code={completed.returncode}){suffix}"
            )

        match = re.search(r"\bcodex-cli\s+([^\s]+)", output)
        actual_version = match.group(1) if match else None
        if actual_version != PINNED_CODEX_RUNTIME_VERSION:
            found = actual_version or "unknown"
            raise InfrastructureError(
                "Project-local Codex runtime version mismatch "
                f"(required={PINNED_CODEX_RUNTIME_VERSION}, found={found}); "
                f"run `{install_command}` from the repository root"
            )

        self._runtime_verified = True

    def _select_thread(self, thread: Any, operation: str) -> str:
        thread_id = getattr(thread, "id", None)
        if not isinstance(thread_id, str) or not thread_id:
            raise InfrastructureError(
                f"Codex returned no thread ID for the {operation} thread"
            )

        self._thread = thread
        self._thread_id = thread_id
        return thread_id

    def _shutdown(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        context = self._codex_context
        self._codex_context = None
        self._codex = None
        self._thread = None
        self._thread_id = None
        self._preflight_complete = False
        self._runtime_verified = False

        if context is None:
            return

        try:
            context.__exit__(exc_type, exc, traceback)
        except Exception as close_error:
            raise self._error("Unable to close the Codex App Server", close_error) from close_error

    def _discard_codex(self) -> None:
        try:
            self._shutdown(None, None, None)
        except InfrastructureError:
            pass

    @staticmethod
    def _error(action: str, exc: Exception) -> InfrastructureError:
        if isinstance(exc, InfrastructureError):
            return exc
        detail = redact_sensitive_text(str(exc)).strip()
        suffix = f": {detail[:2_000]}" if detail else ""
        return InfrastructureError(
            f"{action} ({type(exc).__name__}){suffix}"
        )


def _native_codex_runtime(runtime_path: Path) -> Path:
    """Resolve the native binary behind the pinned npm launcher."""

    resolved = runtime_path.expanduser().resolve()
    if resolved.name != "codex.js":
        return resolved
    target = {
        ("Darwin", "arm64"): ("codex-darwin-arm64", "aarch64-apple-darwin"),
        ("Darwin", "x86_64"): ("codex-darwin-x64", "x86_64-apple-darwin"),
    }.get((platform.system(), platform.machine()))
    if target is None:
        raise InfrastructureError("Unsupported native Codex runtime platform")
    package_name, target_triple = target
    native = resolved.parents[2] / package_name / "vendor" / target_triple / "bin/codex"
    if not native.is_file() or not os.access(native, os.X_OK):
        raise InfrastructureError("Pinned native Codex runtime is unavailable")
    return native.resolve()


def _model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(key): _model_dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_dump(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _model_dump(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value


def _turn_error_detail(value: Any) -> str:
    """Keep a bounded, redacted SDK failure reason for recovery diagnostics."""

    if value is None:
        return ""
    parts: list[str] = []
    for name in ("message", "additional_details"):
        raw = getattr(value, name, None)
        if isinstance(raw, str) and raw.strip():
            parts.append(raw.strip())
    if not parts:
        dumped = _model_dump(value)
        if dumped not in (None, {}, ""):
            parts.append(str(dumped))
    return redact_sensitive_text("; ".join(parts)).strip()[:2_000]


def _final_agent_response(turn: Any) -> str | None:
    messages: list[str] = []
    for wrapped in getattr(turn, "items", []) or []:
        item = getattr(wrapped, "root", wrapped)
        item_type = getattr(item, "type", None)
        if item_type == "agentMessage":
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                messages.append(text)
    return messages[-1] if messages else None


def _agent_message_from_payload(payload: Any) -> str | None:
    item = getattr(payload, "item", None)
    item = getattr(item, "root", item)
    if getattr(item, "type", None) != "agentMessage":
        return None
    text = getattr(item, "text", None)
    return text if isinstance(text, str) and text.strip() else None


def _completed_item_from_payload(payload: Any) -> dict[str, Any] | None:
    raw_item = (
        payload.get("item")
        if isinstance(payload, Mapping)
        else getattr(payload, "item", None)
    )
    return _item_dict(raw_item)


def _item_dict(value: Any) -> dict[str, Any] | None:
    item = getattr(value, "root", value)
    dumped = _model_dump(item)
    if isinstance(dumped, Mapping) and isinstance(dumped.get("root"), Mapping):
        dumped = dumped["root"]
    if not isinstance(dumped, Mapping):
        return None
    return {str(key): item_value for key, item_value in dumped.items()}


def _contains_item(items: list[dict[str, Any]], candidate: Mapping[str, Any]) -> bool:
    candidate_id = candidate.get("id")
    if candidate_id:
        return any(item.get("id") == candidate_id for item in items)
    return any(item == candidate for item in items)


__all__ = [
    "CodexClient",
    "CodexRunResult",
    "DEFAULT_CODEX_RUNTIME_PATH",
    "PINNED_CODEX_RUNTIME_VERSION",
]
