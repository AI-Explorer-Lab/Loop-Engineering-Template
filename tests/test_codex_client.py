from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_loop import codex_client
from codex_loop.codex_client import CodexClient, CodexRunResult
from codex_loop.models import InfrastructureError


class FakeApprovalMode:
    deny_all = object()


class FakeSandbox:
    workspace_write = object()


class FakeTurnStatus:
    completed = "completed"
    failed = "failed"


class FakeCodexConfig:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class FakePolicy:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.staged_source: Path | None = None
        self.retire_calls = 0

    def environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.root / "home"),
            "PATH": "/safe/bin",
        }

    def config_overrides(self) -> tuple[str, ...]:
        return (
            'default_permissions="loop-harness"',
            'approval_policy="never"',
        )

    def app_server_command_prefix(self) -> tuple[str, ...]:
        return (
            "/usr/bin/sandbox-exec",
            "-f",
            str(self.root / "app-server.sb"),
            "--",
        )

    def stage_app_server_executable(self, source: Path) -> Path:
        self.staged_source = source
        return self.root / "app-server-bin/codex"

    def retire_app_server_material(self) -> None:
        self.retire_calls += 1


def completed_turn(text: str = "done", *, turn_id: str = "turn-1") -> Any:
    message = SimpleNamespace(type="agentMessage", text=text)
    return SimpleNamespace(
        id=turn_id,
        status=FakeTurnStatus.completed,
        error=None,
        items=[SimpleNamespace(root=message)],
    )


class FakeHandle:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def stream(self):
        for value in self.values:
            if isinstance(value, Exception):
                raise value
            yield value


class FakeThread:
    def __init__(
        self,
        thread_id: str,
        *,
        cwd: Path,
        streams: list[list[Any]] | None = None,
        history: list[Any] | None = None,
    ) -> None:
        self.id = thread_id
        self.cwd = cwd.resolve()
        self.streams = list(streams or [])
        self.history = list(history or [])
        self.turn_calls: list[tuple[str, dict[str, Any]]] = []

    def turn(self, prompt: str, **kwargs: Any) -> FakeHandle:
        self.turn_calls.append((prompt, kwargs))
        return FakeHandle(self.streams.pop(0))

    def read(self, *, include_turns: bool = False) -> Any:
        if include_turns:
            return SimpleNamespace(thread=SimpleNamespace(turns=self.history))
        return SimpleNamespace(
            thread=SimpleNamespace(cwd=SimpleNamespace(root=str(self.cwd)))
        )


class FakeRawClient:
    def __init__(self, effective_config: dict[str, Any]) -> None:
        self.effective_config = effective_config
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self._approval_handler = lambda _method, _params: {"decision": "accept"}

    def request(self, method: str, params: dict[str, Any], *, response_model: Any):
        self.calls.append((method, params, response_model))
        config = SimpleNamespace(
            model_dump=lambda **_kwargs: dict(self.effective_config)
        )
        return SimpleNamespace(config=config)


class FakeCodex:
    def __init__(
        self,
        root: Path,
        *,
        account: Any = object(),
        start_thread: FakeThread | None = None,
        resume_thread: FakeThread | None = None,
        account_error: Exception | None = None,
        effective_config: dict[str, Any] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.account_value = account
        self.start_thread_value = start_thread or FakeThread("thr-started", cwd=root)
        self.resume_thread_value = resume_thread or FakeThread("thr-resumed", cwd=root)
        self.account_error = account_error
        self._client = FakeRawClient(effective_config or {"ok": True})
        self.entered = False
        self.closed = False
        self.account_calls = 0
        self.start_kwargs: dict[str, Any] | None = None
        self.resume_args: tuple[str, dict[str, Any]] | None = None
        self.config: Any | None = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_args: Any) -> None:
        self.closed = True

    def account(self) -> Any:
        self.account_calls += 1
        if self.account_error:
            raise self.account_error
        return SimpleNamespace(account=self.account_value)

    def thread_start(self, **kwargs: Any) -> FakeThread:
        self.start_kwargs = kwargs
        return self.start_thread_value

    def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.resume_args = (thread_id, kwargs)
        return self.resume_thread_value


def install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeCodex,
    *,
    bypass_runtime_check: bool = True,
) -> None:
    def create_codex(config: Any) -> FakeCodex:
        fake.config = config
        return fake

    sdk_module = SimpleNamespace(
        Codex=create_codex,
        CodexConfig=FakeCodexConfig,
        ApprovalMode=FakeApprovalMode,
        Sandbox=FakeSandbox,
    )
    types_module = SimpleNamespace(TurnStatus=FakeTurnStatus)
    generated_module = SimpleNamespace(ConfigReadResponse=object)

    def fake_import(name: str) -> Any:
        return {
            "openai_codex": sdk_module,
            "openai_codex.types": types_module,
            "openai_codex.generated.v2_all": generated_module,
        }[name]

    monkeypatch.setattr(codex_client, "import_module", fake_import)
    if bypass_runtime_check:
        monkeypatch.setattr(CodexClient, "_verify_runtime", lambda self: None)


def completed_stream(text: str = "done") -> list[Any]:
    turn = completed_turn(text)
    usage = SimpleNamespace(
        method="thread/tokenUsage/updated",
        payload=SimpleNamespace(token_usage={"total_tokens": 10}),
    )
    completed = SimpleNamespace(
        method="turn/completed", payload=SimpleNamespace(turn=turn)
    )
    return [usage, completed]


def test_missing_sdk_is_an_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        codex_client,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ModuleNotFoundError("openai_codex")),
    )

    with pytest.raises(InfrastructureError, match="SDK is unavailable"):
        CodexClient(tmp_path).preflight()


def test_preflight_checks_account_once_and_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeCodex(tmp_path)
    install_fake_sdk(monkeypatch, fake)

    with CodexClient(tmp_path) as client:
        client.preflight()
        assert fake.account_calls == 1
        assert fake.config.experimental_api is False
        assert fake.config.codex_bin == str(
            codex_client.DEFAULT_CODEX_RUNTIME_PATH.resolve()
        )

    assert fake.closed is True
    assert client.thread_id is None


def test_policy_launch_uses_empty_environment_instead_of_sdk_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeCodex(tmp_path)
    install_fake_sdk(monkeypatch, fake)
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.setenv("SERVICE_TOKEN", "must-not-reach-app-server")

    with CodexClient(tmp_path, policy=FakePolicy(tmp_path)):
        command = fake.config.launch_args_override

    assert command[:6] == (
        "/usr/bin/sandbox-exec",
        "-f",
        str(tmp_path / "app-server.sb"),
        "--",
        "/usr/bin/env",
        "-i",
    )
    assert f"HOME={tmp_path / 'home'}" in command
    assert "PATH=/safe/bin" in command
    assert not any(value.startswith("CODEX_SANDBOX=") for value in command)
    assert not any(value.startswith("SERVICE_TOKEN=") for value in command)
    assert command[-7:] == (
        "--config",
        'default_permissions="loop-harness"',
        "--config",
        'approval_policy="never"',
        "app-server",
        "--listen",
        "stdio://",
    )


def test_preflight_rejects_missing_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeCodex(tmp_path, account=None)
    install_fake_sdk(monkeypatch, fake)

    with pytest.raises(InfrastructureError, match="not authenticated"):
        CodexClient(tmp_path).preflight()
    assert fake.closed is True


def test_runtime_version_is_pinned_and_passed_to_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "codex"
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime.chmod(0o755)
    fake = FakeCodex(tmp_path)
    install_fake_sdk(monkeypatch, fake, bypass_runtime_check=False)
    monkeypatch.setattr(
        codex_client.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="codex-cli 0.144.4\n", stderr=""
        ),
    )

    with CodexClient(tmp_path, runtime_path=runtime):
        assert fake.config.codex_bin == str(runtime.resolve())


def test_runtime_version_mismatch_is_an_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "codex"
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime.chmod(0o755)
    fake = FakeCodex(tmp_path)
    install_fake_sdk(monkeypatch, fake, bypass_runtime_check=False)
    monkeypatch.setattr(
        codex_client.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="codex-cli 0.143.0\n", stderr=""
        ),
    )

    with pytest.raises(InfrastructureError, match="version mismatch"):
        CodexClient(tmp_path, runtime_path=runtime).preflight()


def test_threads_and_turn_use_deny_all_and_stream_every_notification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stream = completed_stream("fixed")
    thread = FakeThread("thr-one", cwd=tmp_path, streams=[stream])
    fake = FakeCodex(tmp_path, start_thread=thread)
    install_fake_sdk(monkeypatch, fake)
    seen: list[Any] = []
    denials: list[tuple[str, dict[str, Any]]] = []
    client = CodexClient(
        tmp_path,
        event_sink=seen.append,
        permission_denial_sink=lambda method, params: denials.append(
            (method, dict(params))
        ),
    )

    assert client.start_thread() == "thr-one"
    result = client.run("make the change")

    assert fake.start_kwargs == {
        "cwd": str(tmp_path.resolve()),
        "approval_mode": FakeApprovalMode.deny_all,
    }
    assert thread.turn_calls == [
        (
            "make the change",
            {
                "approval_mode": FakeApprovalMode.deny_all,
                "cwd": str(tmp_path.resolve()),
            },
        )
    ]
    assert seen == stream
    approval = fake._client._approval_handler(
        "item/fileChange/requestApproval",
        {"path": "/outside/task-worktree"},
    )
    assert approval == {"decision": "decline"}
    assert denials == [
        (
            "item/fileChange/requestApproval",
            {"path": "/outside/task-worktree"},
        )
    ]
    assert result == CodexRunResult(
        thread_id="thr-one",
        final_response="fixed",
        turn_id="turn-1",
        usage={"total_tokens": 10},
        items=[{"type": "agentMessage", "text": "fixed"}],
    )


def test_resume_verifies_workspace_effective_config_and_turn_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    turn = completed_turn("saved response", turn_id="turn-saved")
    thread = FakeThread("thr-saved", cwd=tmp_path, history=[turn])
    fake = FakeCodex(
        tmp_path,
        resume_thread=thread,
        effective_config={"default_permissions": "loop-harness"},
    )
    install_fake_sdk(monkeypatch, fake)
    client = CodexClient(tmp_path)

    assert client.resume_thread("thr-saved") == "thr-saved"
    client.verify_thread_workspace()
    assert client.effective_config() == {"default_permissions": "loop-harness"}
    assert client.verify_turn_completed(1) == CodexRunResult(
        thread_id="thr-saved",
        final_response="saved response",
        turn_id="turn-saved",
        usage=None,
        items=[{"type": "agentMessage", "text": "saved response"}],
    )


def test_stream_without_completed_turn_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    thread = FakeThread("thr-one", cwd=tmp_path, streams=[[SimpleNamespace(method="item/completed", payload={})]])
    fake = FakeCodex(tmp_path, start_thread=thread)
    install_fake_sdk(monkeypatch, fake)
    client = CodexClient(tmp_path)
    client.start_thread()

    with pytest.raises(InfrastructureError, match="without turn/completed"):
        client.run("make the change")


def test_failed_turn_preserves_redacted_sdk_error_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failed_turn = SimpleNamespace(
        id="turn-failed",
        status=FakeTurnStatus.failed,
        error=SimpleNamespace(
            message="Invalid output schema; token=super-secret",
            additional_details=None,
        ),
        items=[],
    )
    stream = [
        SimpleNamespace(
            method="turn/completed",
            payload=SimpleNamespace(turn=failed_turn),
        )
    ]
    thread = FakeThread("thr-one", cwd=tmp_path, streams=[stream])
    fake = FakeCodex(tmp_path, start_thread=thread)
    install_fake_sdk(monkeypatch, fake)
    client = CodexClient(tmp_path)
    client.start_thread()

    with pytest.raises(InfrastructureError) as caught:
        client.run("make the change")

    message = str(caught.value)
    assert "Invalid output schema" in message
    assert "super-secret" not in message
    assert "[REDACTED]" in message


def test_visible_response_comes_from_completed_stream_item_when_turn_items_unloaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    message = SimpleNamespace(type="agentMessage", text="streamed final response")
    item_completed = SimpleNamespace(
        method="item/completed",
        payload=SimpleNamespace(item=SimpleNamespace(root=message)),
    )
    unloaded_turn = SimpleNamespace(
        id="turn-unloaded",
        status=FakeTurnStatus.completed,
        error=None,
        items=[],
    )
    turn_completed = SimpleNamespace(
        method="turn/completed",
        payload=SimpleNamespace(turn=unloaded_turn),
    )
    thread = FakeThread(
        "thr-one", cwd=tmp_path, streams=[[item_completed, turn_completed]]
    )
    fake = FakeCodex(tmp_path, start_thread=thread)
    install_fake_sdk(monkeypatch, fake)
    client = CodexClient(tmp_path)
    client.start_thread()

    assert client.run("make the change").final_response == "streamed final response"


def test_runtime_version_pin_matches_package_and_lock_file() -> None:
    orchestrator_root = Path(__file__).resolve().parents[1]
    package = json.loads((orchestrator_root / "package.json").read_text())
    lock = json.loads((orchestrator_root / "package-lock.json").read_text())
    expected = codex_client.PINNED_CODEX_RUNTIME_VERSION

    assert package["dependencies"]["@openai/codex"] == expected
    assert lock["packages"][""]["dependencies"]["@openai/codex"] == expected
    assert lock["packages"]["node_modules/@openai/codex"]["version"] == expected
