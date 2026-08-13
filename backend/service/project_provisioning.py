"""Create a new local Git project with Harness runtime isolation defaults."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..exceptions.business_exception import BusinessException


HARNESS_DIRECTORY = ".harness"
HARNESS_CONFIG_FILE = "project.json"

PROJECT_GITIGNORE = """# Sensitive credentials and temporary runtime files
.codex-runtime/
.codex-orchestrator/active.lock
.codex-orchestrator/worktrees/
.codex-orchestrator/runs/
.codex-orchestrator/queues/
.codex-orchestrator/drafts/
.codex-orchestrator/memory/
.codex-orchestrator/notifications.json

# Python
__pycache__/
*.py[cod]
.pytest_cache/
"""


class ProjectProvisioningError(BusinessException):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message, status_code=status_code)


def provision_git_project(
    project_path: Path,
    *,
    project_id: str,
    project_name: str,
) -> None:
    """Create one new Git project with tracked Harness configuration."""

    raw_path = project_path.expanduser()
    if not raw_path.is_absolute():
        raise ProjectProvisioningError("project path must be absolute")
    path = raw_path.resolve()
    if path.exists():
        raise ProjectProvisioningError(
            f"project path already exists: {path}", status_code=409
        )

    created = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
        created = True
        (path / ".gitignore").write_text(PROJECT_GITIGNORE, encoding="utf-8")
        harness_directory = path / HARNESS_DIRECTORY
        harness_directory.mkdir()
        (harness_directory / HARNESS_CONFIG_FILE).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "codex-harness-project",
                    "project_id": project_id,
                    "project_name": project_name,
                    "state_root": ".codex-orchestrator",
                    "secure_runtime_root": ".codex-runtime",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        _run_git(path, "init")
        _run_git(path, "add", ".gitignore", f"{HARNESS_DIRECTORY}/{HARNESS_CONFIG_FILE}")
        _run_git(
            path,
            "-c",
            "user.name=Loop Engineering",
            "-c",
            "user.email=loop-engineering@localhost",
            "commit",
            "--no-verify",
            "-m",
            "Initialize project",
        )
    except ProjectProvisioningError:
        if created:
            shutil.rmtree(path, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if created:
            shutil.rmtree(path, ignore_errors=True)
        raise ProjectProvisioningError(
            f"project initialization failed: {type(exc).__name__}", status_code=500
        ) from exc


def _run_git(path: Path, *args: str) -> None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise ProjectProvisioningError("Git is unavailable") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise ProjectProvisioningError(
            "Git project initialization failed"
            + (f": {detail[-1][:500]}" if detail else "")
        )


__all__ = [
    "HARNESS_CONFIG_FILE",
    "HARNESS_DIRECTORY",
    "PROJECT_GITIGNORE",
    "ProjectProvisioningError",
    "provision_git_project",
]
