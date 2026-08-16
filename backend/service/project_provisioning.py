"""Create a new local Git project with Harness runtime isolation defaults."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from codex_loop.backend_architecture_bootstrap import materialize_backend_scaffold

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

# Node
node_modules/
dist/
coverage/
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
    backend_architecture_enabled: bool = False,
    frontend_port: int = 8300,
    backend_port: int = 18300,
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
        if backend_architecture_enabled:
            materialize_backend_scaffold(
                path,
                project_name=project_name,
            )
            _write_backend_requirements(path)
        _write_start_script(
            path,
            project_name=project_name,
            project_type=project_type,
            frontend_port=frontend_port,
            backend_port=backend_port,
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
        scripts["dev"] = "vite --host 127.0.0.1"
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


def _write_backend_requirements(path: Path) -> None:
    requirements = path / "requirements.txt"
    if requirements.exists():
        return
    requirements.write_text(
        "-r backend/requirements.txt\npytest>=8.0,<10.0\n",
        encoding="utf-8",
    )


def _write_start_script(
    path: Path,
    *,
    project_name: str,
    project_type: str,
    frontend_port: int,
    backend_port: int,
) -> None:
    normalized_type = str(project_type).strip().lower()
    has_frontend = normalized_type in {"frontend", "fullstack"}
    has_backend = normalized_type in {"python", "fullstack"}
    if not has_frontend and not has_backend:
        raise ProjectProvisioningError(
            f"unsupported project type for start.sh: {project_type}"
        )

    environment_name = f"loop-project-{project_name.lower()}"
    lines = [
        "#!/usr/bin/env bash",
        "",
        "set -Eeuo pipefail",
        "",
        'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        f'FRONTEND_PORT="${{FRONTEND_PORT:-{int(frontend_port)}}}"',
        f'BACKEND_PORT="${{BACKEND_PORT:-{int(backend_port)}}}"',
        'FRONTEND_PID=""',
        'BACKEND_PID=""',
        "",
        'fail() { printf \'启动失败：%s\\n\' "$1" >&2; exit 1; }',
        "",
        "cleanup() {",
        "  local exit_code=$?",
        "  trap - EXIT INT TERM",
        '  for pid in "${FRONTEND_PID}" "${BACKEND_PID}"; do',
        '    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then kill "${pid}" 2>/dev/null || true; fi',
        "  done",
        '  for pid in "${FRONTEND_PID}" "${BACKEND_PID}"; do',
        '    if [[ -n "${pid}" ]]; then wait "${pid}" 2>/dev/null || true; fi',
        "  done",
        '  exit "${exit_code}"',
        "}",
        "",
        "trap cleanup EXIT",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
    ]
    if has_frontend:
        lines.extend(
            [
                "command -v npm >/dev/null 2>&1 || fail \"未找到 npm。\"",
                '[[ -x "${ROOT}/frontend/node_modules/.bin/vite" ]] || fail "前端依赖未安装，请先运行 npm ci --prefix frontend。"',
                "(",
                '  cd "${ROOT}/frontend"',
                '  npm run dev -- --port "${FRONTEND_PORT}" --strictPort',
                ") &",
                "FRONTEND_PID=$!",
            ]
        )
    if has_backend:
        lines.extend(
            [
                "command -v conda >/dev/null 2>&1 || fail \"未找到 conda。\"",
                f"conda run -n {environment_name} python -c 'import fastapi, uvicorn' >/dev/null 2>&1 || fail \"后端 Conda 环境缺少 FastAPI/Uvicorn 依赖。\"",
                f'conda run -n {environment_name} python -m uvicorn backend.main:app --host 127.0.0.1 --port "${{BACKEND_PORT}}" &',
                "BACKEND_PID=$!",
            ]
        )
    lines.extend(
        [
            "",
            'printf \'项目已启动：前端端口 %s，后端端口 %s\\n\' "${FRONTEND_PORT}" "${BACKEND_PORT}"',
            "",
            "while true; do",
            '  for pid in "${FRONTEND_PID}" "${BACKEND_PID}"; do',
            '    if [[ -n "${pid}" ]] && ! kill -0 "${pid}" 2>/dev/null; then wait "${pid}" || true; exit 1; fi',
            "  done",
            "  sleep 1",
            "done",
            "",
        ]
    )
    start_script = path / "start.sh"
    start_script.write_text("\n".join(lines), encoding="utf-8")
    start_script.chmod(0o755)


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
