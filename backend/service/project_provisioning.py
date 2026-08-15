"""Create a new local Git project with Harness runtime isolation defaults."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
    project_type: str = "python",
    validation_options: list[str] | None = None,
    required_paths: list[str] | None = None,
    commit_initial_state: bool = True,
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
        _write_project_scaffold(
            path,
            project_type=project_type,
            validation_options=validation_options or [],
        )
        missing = [
            relative
            for relative in required_paths or []
            if not (path / relative).exists()
        ]
        if missing:
            raise ProjectProvisioningError(
                "project scaffold is missing required paths: " + ", ".join(missing)
            )
        _run_git(path, "init", "-b", "main")
        if commit_initial_state:
            commit_project_state(path)
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


def commit_project_state(project_path: Path) -> None:
    """Commit the scaffold plus any dependency lock files created during bootstrap."""

    _run_git(project_path, "add", ".")
    _run_git(
        project_path,
        "-c",
        "user.name=Loop Engineering",
        "-c",
        "user.email=loop-engineering@localhost",
        "commit",
        "--no-verify",
        "-m",
        "Initialize project",
    )


def _write_project_scaffold(
    path: Path,
    *,
    project_type: str,
    validation_options: list[str],
) -> None:
    """Write only the selected, minimal files needed by the validation profile."""

    selected = set(validation_options)
    if "python_tests" in selected:
        tests = path / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_smoke.py").write_text(
            "import unittest\n\n\nclass SmokeTest(unittest.TestCase):\n    def test_project_bootstrap(self) -> None:\n        self.assertTrue(True)\n\n\nif __name__ == \"__main__\":\n    unittest.main()\n",
            encoding="utf-8",
        )

    frontend_options = {
        "frontend_tests",
        "frontend_typecheck",
        "frontend_build",
    }
    if selected & frontend_options:
        frontend = path / "frontend"
        (frontend / "src").mkdir(parents=True, exist_ok=True)
        package: dict[str, Any] = {
            "name": path.name.lower().replace("_", "-"),
            "private": True,
            "version": "0.1.0",
            "type": "module",
            "scripts": {},
            "devDependencies": {},
        }
        scripts = package["scripts"]
        dev_dependencies = package["devDependencies"]
        if "frontend_tests" in selected:
            scripts["test"] = "vitest run"
            dev_dependencies.update(
                {
                    "@vitejs/plugin-vue": "^6.0.8",
                    "@vue/test-utils": "^2.4.6",
                    "jsdom": "^26.1.0",
                    "typescript": "~5.9.3",
                    "vite": "^8.1.4",
                    "vitest": "^3.2.4",
                }
            )
            (frontend / "vitest.config.ts").write_text(
                'import { defineConfig } from "vitest/config";\n\nexport default defineConfig({\n  test: { environment: "node" },\n});\n',
                encoding="utf-8",
            )
            (frontend / "src" / "smoke.test.ts").write_text(
                'import { describe, expect, it } from "vitest";\n\ndescribe("project bootstrap", () => {\n  it("has a working test runner", () => {\n    expect(true).toBe(true);\n  });\n});\n',
                encoding="utf-8",
            )
        if "frontend_typecheck" in selected:
            scripts["typecheck"] = "tsc --noEmit"
            dev_dependencies["typescript"] = "~5.9.3"
            (frontend / "tsconfig.json").write_text(
                '{\n  "compilerOptions": {\n    "target": "ES2022",\n    "module": "ESNext",\n    "moduleResolution": "Bundler",\n    "strict": true,\n    "noEmit": true\n  },\n  "include": ["src/**/*.ts"]\n}\n',
                encoding="utf-8",
            )
        if "frontend_build" in selected:
            scripts["build"] = "vite build"
            dev_dependencies.update(
                {
                    "@vitejs/plugin-vue": "^6.0.8",
                    "vite": "^8.1.4",
                }
            )
            (frontend / "index.html").write_text(
                '<!doctype html>\n<html><head><meta charset="UTF-8"><title>Project</title></head><body><div id="app"></div><script type="module" src="/src/main.ts"></script></body></html>\n',
                encoding="utf-8",
            )
            (frontend / "vite.config.ts").write_text(
                'import { defineConfig } from "vite";\n\nexport default defineConfig({});\n',
                encoding="utf-8",
            )
            (frontend / "src" / "main.ts").write_text(
                'const root = document.querySelector<HTMLDivElement>("#app");\nif (root) root.textContent = "Project bootstrap";\n',
                encoding="utf-8",
            )
        (frontend / "package.json").write_text(
            json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


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
