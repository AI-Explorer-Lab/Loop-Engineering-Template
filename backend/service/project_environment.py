"""Prepare project-owned frontend dependencies and Python environments."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Sequence

from ..exceptions.business_exception import ProjectConfigurationError


PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")
PYTHON_VERSION = "3.12"


@dataclass(frozen=True, slots=True)
class ProjectEnvironment:
    conda_env_name: str | None
    frontend_install_command: tuple[str, ...] | None


def validate_project_name(name: str) -> str:
    normalized = str(name).strip()
    if not PROJECT_NAME_PATTERN.fullmatch(normalized):
        raise ProjectConfigurationError(
            "项目名称只能包含 ASCII 字母、数字和连字符，长度为 1-64 个字符，且不能以连字符开头或结尾"
        )
    return normalized


def conda_environment_name(project_name: str) -> str:
    return f"loop-project-{validate_project_name(project_name).lower()}"


def prepare_project_environment(
    project_root: str | Path,
    *,
    project_name: str,
    project_type: str,
    validation_options: Sequence[str],
) -> ProjectEnvironment:
    """Install trusted project dependencies before the project is registered."""

    root = Path(project_root).expanduser().resolve()
    selected = {str(value).strip() for value in validation_options}
    conda_name: str | None = None
    if "python_tests" in selected:
        conda_name = conda_environment_name(project_name)
        _ensure_conda_environment(conda_name)
        _install_python_dependencies(root, conda_name)

    frontend_command: tuple[str, ...] | None = None
    if selected & {"frontend_tests", "frontend_typecheck", "frontend_build"}:
        frontend_root = root / "frontend"
        if not (frontend_root / "package.json").is_file():
            raise ProjectConfigurationError("frontend/package.json is required")
        if (frontend_root / "package-lock.json").is_file():
            command = ("npm", "ci")
        else:
            command = ("npm", "install")
        _run(command, cwd=frontend_root)
        if not (frontend_root / "package-lock.json").is_file():
            raise ProjectConfigurationError(
                "npm install completed without generating frontend/package-lock.json"
            )
        frontend_command = command

    return ProjectEnvironment(
        conda_env_name=conda_name,
        frontend_install_command=frontend_command,
    )


def _ensure_conda_environment(environment_name: str) -> None:
    conda = shutil.which("conda") or os.environ.get("CONDA_EXE")
    if not conda:
        raise ProjectConfigurationError("Conda is required for a Python project")
    probe = subprocess.run(
        [
            conda,
            "run",
            "-n",
            environment_name,
            "python",
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)",
        ],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if probe.returncode == 0:
        return
    install = subprocess.run(
        [conda, "install", "-n", environment_name, f"python={PYTHON_VERSION}", "-y"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if install.returncode == 0:
        return
    _run(
        (conda, "create", "-n", environment_name, f"python={PYTHON_VERSION}", "-y"),
        cwd=None,
    )


def _install_python_dependencies(root: Path, environment_name: str) -> None:
    conda = shutil.which("conda") or os.environ.get("CONDA_EXE")
    assert conda
    requirements = root / "requirements.txt"
    pyproject = root / "pyproject.toml"
    if requirements.is_file():
        _run(
            (conda, "run", "-n", environment_name, "python", "-m", "pip", "install", "-r", "requirements.txt"),
            cwd=root,
        )
    elif pyproject.is_file():
        _run(
            (conda, "run", "-n", environment_name, "python", "-m", "pip", "install", "."),
            cwd=root,
        )
    _run(
        (conda, "run", "-n", environment_name, "python", "-m", "pip", "install", "pytest"),
        cwd=root,
    )


def _run(command: Sequence[str], *, cwd: Path | None) -> None:
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ProjectConfigurationError(
            f"项目环境初始化失败（{' '.join(str(part) for part in command[:4])}）{suffix}"
        )
