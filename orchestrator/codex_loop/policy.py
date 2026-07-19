"""Least-privilege Codex and validation policy for one task worktree."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sys
from typing import Any, Mapping, Sequence

from .models import InfrastructureError, SCHEMA_VERSION, utc_now_iso
from .workspace import WorkspaceInfo


POLICY_NAME = "loop-harness"
_SENSITIVE_ENVIRONMENT = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|DATABASE_URL|DB_URL|AWS_|AZURE_|"
    r"GOOGLE_|GCP_|KUBECONFIG|KUBERNETES|SSH_|DOCKER_HOST)",
    re.IGNORECASE,
)
_SAFE_ENVIRONMENT_NAMES = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "CONDA_EXE",
    "CONDA_PYTHON_EXE",
}
PRODUCTION_COMMANDS = {
    "ansible",
    "aws",
    "az",
    "docker",
    "gcloud",
    "helm",
    "kubectl",
    "mysql",
    "pg_dump",
    "psql",
    "scp",
    "ssh",
    "terraform",
}
NETWORK_COMMANDS = {"curl", "ftp", "nc", "ncat", "telnet", "wget"}
GIT_MUTATING_COMMANDS = {
    "checkout",
    "clean",
    "commit",
    "merge",
    "pull",
    "push",
    "rebase",
    "reset",
    "switch",
    "tag",
}
WRITABLE_NODE_CACHE_NAMES = {".cache", ".tmp", ".vite", ".vite-temp"}
DEFAULT_DEPENDENCY_PATHS = (
    Path("frontend/node_modules"),
    Path("orchestrator/node_modules"),
    Path("orchestrator/frontend/node_modules"),
)


class ExecutionPolicy:
    """Build, verify, and serialize the requested/effective task policy."""

    def __init__(
        self,
        control_repo_root: str | Path,
        workspace: WorkspaceInfo,
        *,
        dependency_paths: Sequence[str | Path] = DEFAULT_DEPENDENCY_PATHS,
    ) -> None:
        self.control_repo_root = Path(control_repo_root).expanduser().resolve()
        self.workspace = workspace
        self.dependency_paths = tuple(
            self._safe_dependency_path(path) for path in dependency_paths
        )
        self.runtime_root = workspace.worktree / ".codex-runtime"
        self.home_dir = self.runtime_root / "home"
        self.codex_home_dir = self.runtime_root / "codex-home"
        self.tmp_dir = self.runtime_root / "tmp"
        self.cache_dir = self.runtime_root / "cache"
        self.app_server_profile_path = self.runtime_root / "app-server.sb"
        self.app_server_bin_dir = self.runtime_root / "app-server-bin"
        self.app_server_executable_path = self.app_server_bin_dir / "codex"

    def prepare_runtime(self) -> None:
        for directory in (
            self.home_dir,
            self.codex_home_dir,
            self.tmp_dir,
            self.cache_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)

        # Copy only the control-plane login material. Personal config, plugins,
        # memories, and state databases are intentionally not inherited. The
        # copied file lives in an ignored runtime directory and is denied to
        # task commands by the permission profile below.
        source_codex_home = Path(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
        ).expanduser()
        source_auth = source_codex_home / "auth.json"
        target_auth = self.codex_home_dir / "auth.json"
        if source_auth.is_file():
            shutil.copyfile(source_auth, target_auth)
            target_auth.chmod(0o600)

        # Worktrees do not carry ignored dependencies. Reuse each installed
        # top-level package through links inside a real ``node_modules``
        # directory. Keeping the directory itself real matters because the
        # repository's ``node_modules/`` ignore rules do not match a directory
        # symlink, which would make a pristine task worktree appear dirty.
        for relative in self.dependency_paths:
            source = self.control_repo_root / relative
            target = self.workspace.worktree / relative
            if not source.is_dir():
                continue
            if target.is_symlink():
                target.unlink()
            target.mkdir(parents=True, exist_ok=True)
            for cache_name in WRITABLE_NODE_CACHE_NAMES:
                cache_path = target / cache_name
                if cache_path.is_symlink():
                    cache_path.unlink()
                if cache_path.exists() and not cache_path.is_dir():
                    raise InfrastructureError(
                        f"Node cache path is not a directory: {relative / cache_name}"
                    )
                cache_path.mkdir(exist_ok=True)
            for source_entry in source.iterdir():
                if source_entry.name in WRITABLE_NODE_CACHE_NAMES:
                    continue
                target_entry = target / source_entry.name
                if target_entry.exists() or target_entry.is_symlink():
                    continue
                target_entry.symlink_to(
                    source_entry,
                    target_is_directory=source_entry.is_dir(),
                )

        if platform.system() == "Darwin":
            self._write_app_server_profile()

    def environment(self) -> dict[str, str]:
        source = os.environ
        environment: dict[str, str] = {}
        for name, value in source.items():
            if _SENSITIVE_ENVIRONMENT.search(name):
                continue
            if name in _SAFE_ENVIRONMENT_NAMES or name.startswith("LC_"):
                environment[name] = value
        environment["HOME"] = str(self.home_dir)
        environment["CODEX_HOME"] = str(self.codex_home_dir)
        environment["TMPDIR"] = str(self.tmp_dir)
        environment["XDG_CACHE_HOME"] = str(self.cache_dir)
        environment["PYTHONPYCACHEPREFIX"] = str(self.cache_dir / "python")
        environment["npm_config_cache"] = str(self.cache_dir / "npm")
        environment["CI"] = "true"
        developer_dir = _developer_directory()
        if developer_dir is not None:
            environment["DEVELOPER_DIR"] = str(developer_dir)
            developer_bin = developer_dir / "usr/bin"
            if developer_bin.is_dir():
                environment["PATH"] = os.pathsep.join(
                    (str(developer_bin), environment.get("PATH", ""))
                ).rstrip(os.pathsep)
        return environment

    def validation_environment(self) -> dict[str, str]:
        """Return the task environment without App Server login metadata."""

        environment = self.environment()
        environment.pop("CODEX_HOME", None)
        return environment

    def config_overrides(self) -> tuple[str, ...]:
        # codex-cli 0.144.4 always adds the host shared-temp directory to
        # restrictive custom profiles. A restrictive outer Seatbelt cannot
        # be combined with that inner profile because macOS then rejects the
        # sandbox reinitialization. Resolve this named profile to unrestricted
        # execution *inside* the single external Seatbelt boundary below.
        filesystem = {":root": "write"}
        profile = {
            "description": "Externally sandboxed task worktree",
            "extends": ":read-only",
            "filesystem": filesystem,
            "network": {"enabled": True},
        }
        return (
            f"default_permissions={json.dumps(POLICY_NAME)}",
            f"permissions.{POLICY_NAME}={_toml_inline(profile)}",
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

    def app_server_command_prefix(self) -> tuple[str, ...]:
        """Return the outer filesystem sandbox for the App Server process tree."""

        if platform.system() != "Darwin":
            raise InfrastructureError(
                "This first-stage App Server sandbox currently requires macOS"
            )
        sandbox_exec = Path("/usr/bin/sandbox-exec")
        if not sandbox_exec.is_file():
            raise InfrastructureError("macOS sandbox-exec is unavailable")
        if not self.app_server_profile_path.is_file():
            raise InfrastructureError("App Server sandbox profile is unavailable")
        return (
            str(sandbox_exec),
            "-f",
            str(self.app_server_profile_path),
            "--",
        )

    def stage_app_server_executable(self, source: str | Path) -> Path:
        """Copy the native App Server binary to its protected runtime path."""

        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file() or not os.access(source_path, os.X_OK):
            raise InfrastructureError("Native Codex App Server runtime is unavailable")
        self.app_server_bin_dir.mkdir(parents=True, exist_ok=True)
        self.app_server_bin_dir.chmod(0o700)
        if self.app_server_executable_path.exists():
            self.app_server_executable_path.unlink()
        shutil.copyfile(source_path, self.app_server_executable_path)
        self.app_server_executable_path.chmod(0o700)
        self._write_app_server_profile()
        return self.app_server_executable_path

    def retire_app_server_material(self) -> None:
        """Remove executable/login material before model commands can run."""

        for path in (
            self.app_server_executable_path,
            self.codex_home_dir / "auth.json",
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise InfrastructureError(
                    f"Unable to retire protected runtime material: {path.name}"
                ) from exc

    def _write_app_server_profile(self) -> None:
        """Write the single external Seatbelt boundary for App Server + tools."""

        readable_paths = self._seatbelt_read_paths()
        readable_files = self._node_package_scope_manifests()
        worktree = str(self.workspace.worktree)
        auth_path = self.codex_home_dir / "auth.json"
        worktree_git_file = self.workspace.worktree / ".git"
        validation_profile = self.runtime_root / "validation.sb"
        app_server_executable = str(self.app_server_executable_path)
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal (target same-sandbox))",
            "(allow mach-lookup)",
            "(allow sysctl-read)",
            "(allow ipc-posix-shm)",
            "(allow file-read-metadata)",
            '(allow file-read-data (literal "/"))',
            f"(allow file-read* (require-all (subpath {json.dumps(worktree)})",
            f"  (require-not (literal {json.dumps(str(auth_path))}))))",
        ]
        lines.extend(
            f"(allow file-read* (subpath {json.dumps(path)}))"
            for path in readable_paths
            if Path(path).exists() and path != worktree
        )
        lines.extend(
            f"(allow file-read* (literal {json.dumps(str(path))}))"
            for path in readable_files
        )
        lines.extend(
            f"(deny process-exec (literal {json.dumps(str(path))}))"
            for path in self._denied_production_executable_paths()
        )
        lines.extend(
            [
                f"(allow file-write* (subpath {json.dumps(worktree)}))",
                '(allow file-write* (literal "/dev/null"))',
                f"(with-filter (process-path {json.dumps(app_server_executable)})",
                "  (allow network*)",
                f"  (allow file-read* (literal {json.dumps(str(auth_path))})))",
                f"(deny file-write* (literal {json.dumps(app_server_executable)}))",
                f"(deny file-write* (literal {json.dumps(str(auth_path))}))",
                f"(deny file-write* (literal {json.dumps(str(self.app_server_profile_path))}))",
                f"(deny file-write* (literal {json.dumps(str(validation_profile))}))",
                f"(deny file-write* (literal {json.dumps(str(worktree_git_file))}))",
            ]
        )
        self.app_server_profile_path.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        self.app_server_profile_path.chmod(0o600)

    def _seatbelt_read_paths(self) -> tuple[str, ...]:
        """Return the fixed system and task runtime paths required by local tools."""

        return tuple(
            dict.fromkeys(
                (
                    "/System",
                    "/usr",
                    "/bin",
                    "/sbin",
                    "/Library",
                    "/private/etc",
                    "/private/var/db/timezone",
                    "/dev",
                    "/opt",
                    str(self.workspace.worktree),
                    str(self.control_repo_root / ".git"),
                    *(
                        str(self.control_repo_root / relative)
                        for relative in self.dependency_paths
                    ),
                    *(str(path) for path in self._runtime_read_paths()),
                )
            )
        )

    def _node_package_scope_manifests(self) -> tuple[Path, ...]:
        """Allow Node to read only package-scope manifests used by shared deps."""

        candidates = [
            self.control_repo_root / relative.parent / "package.json"
            for relative in self.dependency_paths
        ]
        candidates.extend(
            parent / "package.json"
            for parent in (self.control_repo_root, *self.control_repo_root.parents)
        )
        return tuple(
            dict.fromkeys(path for path in candidates if path.is_file())
        )

    def validation_command_prefix(self) -> tuple[str, ...]:
        """Return the OS sandbox wrapper for fixed validation commands."""

        if platform.system() != "Darwin":
            raise InfrastructureError(
                "This first-stage validation sandbox currently requires macOS"
            )
        sandbox_exec = Path("/usr/bin/sandbox-exec")
        if not sandbox_exec.is_file():
            raise InfrastructureError("macOS sandbox-exec is unavailable")
        profile_path = self.runtime_root / "validation.sb"
        readable_paths = self._seatbelt_read_paths()
        readable_files = self._node_package_scope_manifests()
        auth_path = self.codex_home_dir / "auth.json"
        worktree_git_file = self.workspace.worktree / ".git"
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal (target same-sandbox))",
            "(allow mach-lookup)",
            "(allow sysctl-read)",
            "(allow ipc-posix-shm)",
            "(allow file-read-metadata)",
            # CPython enumerates the filesystem root during startup on macOS.
            # This permits only the root directory entry itself, not reading
            # files below otherwise-denied paths.
            '(allow file-read-data (literal "/"))',
        ]
        lines.extend(
            f"(allow file-read* (subpath {json.dumps(path)}))"
            for path in dict.fromkeys(readable_paths)
            if Path(path).exists()
        )
        lines.extend(
            f"(allow file-read* (literal {json.dumps(str(path))}))"
            for path in readable_files
        )
        lines.extend(
            [
                f"(allow file-write* (subpath {json.dumps(str(self.workspace.worktree))}))",
                '(allow file-write* (literal "/dev/null"))',
                f"(deny file-read* (literal {json.dumps(str(auth_path))}))",
                f"(deny file-write* (literal {json.dumps(str(auth_path))}))",
                f"(deny file-write* (literal {json.dumps(str(profile_path))}))",
                f"(deny file-write* (literal {json.dumps(str(worktree_git_file))}))",
            ]
        )
        profile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        profile_path.chmod(0o600)
        return (str(sandbox_exec), "-f", str(profile_path), "--")

    def _runtime_read_paths(self) -> tuple[Path, ...]:
        """Find only the local runtimes required by fixed task commands."""

        candidates: list[Path] = [
            *(
                self.control_repo_root / relative
                for relative in self.dependency_paths
            ),
            Path(sys.executable).expanduser().absolute().parent.parent,
        ]
        developer_dir = _developer_directory()
        if developer_dir is not None:
            candidates.append(developer_dir)
        conda_executable = os.environ.get("CONDA_EXE")
        if conda_executable:
            candidates.append(
                _executable_prefix(Path(conda_executable).expanduser().absolute())
            )
        path_value = os.environ.get("PATH")
        for executable in ("conda", "node", "npm"):
            located = shutil.which(executable, path=path_value)
            if located:
                candidates.append(
                    _executable_prefix(Path(located).expanduser().absolute())
                )
        return tuple(
            dict.fromkeys(path for path in candidates if path.exists())
        )

    @staticmethod
    def _safe_dependency_path(value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("dependency paths must be safe relative paths")
        return path

    def _denied_production_executable_paths(self) -> tuple[Path, ...]:
        """Resolve production CLIs so Seatbelt can deny executing them."""

        paths: list[Path] = []
        search_path = self.environment().get("PATH")
        for command in sorted(PRODUCTION_COMMANDS):
            located = shutil.which(command, path=search_path)
            if located is None:
                continue
            executable = Path(located).expanduser().absolute()
            paths.extend((executable, executable.resolve()))
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _shared_temp_paths() -> tuple[Path, ...]:
        """Return host temp roots, excluding the task-local TMPDIR override."""

        candidates = [Path("/tmp"), Path("/private/tmp")]
        inherited_tmp = os.environ.get("TMPDIR")
        if inherited_tmp:
            candidates.append(Path(inherited_tmp).expanduser())
        return tuple(
            dict.fromkeys(
                path.resolve()
                for path in candidates
                if path.exists()
            )
        )

    def requested_snapshot(self) -> dict[str, Any]:
        relative = self.workspace.worktree_relative_path
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.workspace.task_id,
            "requested": {
                "approval_mode": "deny_all",
                "permission_profile": POLICY_NAME,
                "filesystem": {
                    "workspace_root": relative,
                    "outside_workspace_read": "deny",
                    "outside_workspace_write": "deny",
                    "git_metadata": "read_only",
                    "shared_tmp": "deny",
                },
                "network": "disabled",
                "web_search": "disabled",
                "apps_mcp_plugins": "disabled",
                "multi_agent": "disabled",
                "production_credentials": "removed",
                "production_commands": "deny",
            },
            "effective": {
                "verified": False,
            },
            "verified_at": None,
        }

    def verify_effective(self, effective_config: Mapping[str, Any]) -> dict[str, Any]:
        config = dict(effective_config)
        approval_policy = _normalized_config_value(config.get("approval_policy"))
        if approval_policy != "never":
            raise InfrastructureError(
                "Effective approval policy is wider than deny_all"
            )
        if config.get("default_permissions") != POLICY_NAME:
            raise InfrastructureError("Effective Codex permission profile is unknown")
        if config.get("sandbox_mode") not in {None, ""}:
            raise InfrastructureError(
                "Legacy sandbox_mode overrides the required permission profile"
            )
        permissions = config.get("permissions")
        if not isinstance(permissions, Mapping):
            raise InfrastructureError("Effective Codex permissions are not readable")
        profile = permissions.get(POLICY_NAME)
        if not isinstance(profile, Mapping):
            raise InfrastructureError("Effective task permission profile is missing")
        if profile.get("extends") != ":read-only":
            raise InfrastructureError("Effective permission profile has an unsafe base")
        filesystem = profile.get("filesystem")
        network = profile.get("network")
        if not isinstance(filesystem, Mapping) or filesystem.get(":root") != "write":
            raise InfrastructureError("Effective external-sandbox profile is unknown")
        if not isinstance(network, Mapping) or not bool(network.get("enabled", False)):
            raise InfrastructureError("Effective external-sandbox profile is unknown")
        if str(config.get("web_search", "")) != "disabled":
            raise InfrastructureError("Effective web search policy is wider than requested")
        if not self.app_server_profile_path.is_file():
            raise InfrastructureError("External App Server sandbox is unavailable")
        seatbelt = self.app_server_profile_path.read_text(encoding="utf-8")
        auth_path = self.codex_home_dir / "auth.json"
        required_rules = (
            "(deny default)",
            f'(allow file-write* (subpath "{self.workspace.worktree}"))',
            f'(process-path "{self.app_server_executable_path}")',
            f'(deny file-write* (literal "{auth_path}"))',
        )
        if any(rule not in seatbelt for rule in required_rules):
            raise InfrastructureError("External App Server sandbox is incomplete")

        snapshot = self.requested_snapshot()
        snapshot["effective"] = {
            "approval_mode": "deny_all",
            "permission_profile": POLICY_NAME,
            "sandbox_or_profile": f"{POLICY_NAME}+external-macos-seatbelt",
            "network": "disabled",
            "writable_roots": [self.workspace.worktree_relative_path],
            "environment_policy": "allowlist",
            "verified": True,
        }
        snapshot["verified_at"] = utc_now_iso()
        return snapshot

    @staticmethod
    def denied_command_reason(command: str | list[str]) -> str | None:
        classification = ExecutionPolicy.denied_command_classification(command)
        return classification[1] if classification else None

    @staticmethod
    def denied_command_classification(
        command: str | list[str],
    ) -> tuple[str, str] | None:
        if isinstance(command, str):
            try:
                words = shlex.split(command)
            except ValueError:
                words = command.strip().split()
        else:
            words = [str(word) for word in command]
        if not words:
            return None
        executable = Path(words[0]).name.lower()
        if executable in {"bash", "sh", "zsh"} and len(words) >= 3:
            for index, word in enumerate(words[1:-1], start=1):
                if word in {"-c", "-lc"}:
                    return ExecutionPolicy.denied_command_classification(
                        words[index + 1]
                    )
        if executable in PRODUCTION_COMMANDS:
            return "production", "production or external command is forbidden"
        if executable in NETWORK_COMMANDS:
            return "network", "network command is forbidden"
        if executable == "git" and len(words) > 1 and words[1] in GIT_MUTATING_COMMANDS:
            return "git", "Git branch, commit, and history mutations are forbidden"
        return None


def _toml_inline(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, Mapping):
        return "{" + ",".join(
            f"{json.dumps(str(key))}={_toml_inline(item)}"
            for key, item in value.items()
        ) + "}"
    raise TypeError(f"Unsupported TOML override value: {type(value).__name__}")


def _normalized_config_value(value: Any) -> str:
    """Normalize root-model/enum JSON shapes returned by ``config/read``."""

    if isinstance(value, Mapping) and "root" in value:
        value = value["root"]
    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold().replace("_", "-")


def _executable_prefix(path: Path) -> Path:
    if path.parent.name in {"bin", "condabin"}:
        return path.parent.parent
    return path.parent


def _developer_directory() -> Path | None:
    configured = os.environ.get("DEVELOPER_DIR")
    candidates = [
        Path(configured).expanduser().absolute() if configured else None,
        Path("/Library/Developer/CommandLineTools"),
    ]
    return next(
        (path for path in candidates if path is not None and path.is_dir()),
        None,
    )


__all__ = [
    "ExecutionPolicy",
    "NETWORK_COMMANDS",
    "POLICY_NAME",
    "PRODUCTION_COMMANDS",
]
