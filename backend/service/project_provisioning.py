"""Create a new local Git project with Harness runtime isolation defaults."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..exceptions.business_exception import BusinessException


PROJECT_GITIGNORE = """# Harness and local orchestrator state
.codex-runtime/
.codex-orchestrator/

# Python
__pycache__/
*.py[cod]
.pytest_cache/
"""


class ProjectProvisioningError(BusinessException):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message, status_code=status_code)


def provision_git_project(project_path: Path) -> None:
    """Create one new directory, initialize Git, and create its first commit."""

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
        _run_git(path, "init")
        _run_git(path, "add", ".gitignore")
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


__all__ = ["PROJECT_GITIGNORE", "ProjectProvisioningError", "provision_git_project"]
