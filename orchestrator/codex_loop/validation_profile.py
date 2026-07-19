"""Trusted, project-specific validation configuration.

Validation commands come from the operator-owned Orchestrator configuration,
never from a task prompt or model response.  Every command is represented as
an argument vector and is still executed with ``shell=False`` inside the
existing validation sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TESTS_PLACEHOLDER = "{tests}"
DEFAULT_EXCLUDED_DIRECTORIES = (
    ".cache",
    ".git",
    ".pytest_cache",
    "coverage",
    "dist",
    "node_modules",
    "__pycache__",
)


@dataclass(frozen=True, slots=True)
class ValidationProbe:
    """One bounded preflight command and its user-facing failure message."""

    command: tuple[str, ...]
    unavailable_message: str


@dataclass(frozen=True, slots=True)
class TestGroup:
    """How to discover and run changed tests under one project directory."""

    name: str
    root: Path
    path_base: Path
    suffixes: tuple[str, ...]
    command: tuple[str, ...]
    exclude_directories: tuple[str, ...] = DEFAULT_EXCLUDED_DIRECTORIES

    def contains(self, relative_path: str | Path) -> bool:
        path = Path(relative_path)
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return any(path.name.endswith(suffix) for suffix in self.suffixes)

    def command_for(self, relative_paths: Sequence[str]) -> tuple[str, ...]:
        rendered: list[str] = []
        for part in self.command:
            if part != TESTS_PLACEHOLDER:
                rendered.append(part)
                continue
            for raw_path in relative_paths:
                path = Path(raw_path)
                try:
                    value = path.relative_to(self.path_base).as_posix()
                except ValueError as exc:
                    raise ValueError(
                        f"test path {raw_path!r} is outside path_base for {self.name}"
                    ) from exc
                rendered.append(value if not value.startswith("-") else f"./{value}")
        return tuple(rendered)


@dataclass(frozen=True, slots=True)
class ValidationProfile:
    """Complete trusted validation policy for one managed repository."""

    required_paths: tuple[Path, ...]
    preflight: tuple[ValidationProbe, ...]
    test_groups: tuple[TestGroup, ...]
    full_commands: tuple[tuple[str, ...], ...]
    dependency_paths: tuple[Path, ...]

    @property
    def required_executables(self) -> tuple[str, ...]:
        commands = [probe.command for probe in self.preflight]
        commands.extend(group.command for group in self.test_groups)
        commands.extend(self.full_commands)
        return tuple(sorted({command[0] for command in commands}))

    def contains_test(self, relative_path: str | Path) -> bool:
        return any(group.contains(relative_path) for group in self.test_groups)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
    ) -> "ValidationProfile":
        if not value:
            return default_web_validation_profile()
        if not isinstance(value, Mapping):
            raise ValueError("validation must be an object")

        required_paths = tuple(
            _safe_relative_path(item, "required_paths")
            for item in _sequence(value.get("required_paths", ()), "required_paths")
        )
        dependency_paths = tuple(
            _safe_relative_path(item, "dependency_paths")
            for item in _sequence(
                value.get("dependency_paths", ()), "dependency_paths"
            )
        )

        preflight: list[ValidationProbe] = []
        for index, item in enumerate(
            _sequence(value.get("preflight", ()), "preflight")
        ):
            if not isinstance(item, Mapping):
                raise ValueError(f"validation.preflight[{index}] must be an object")
            command = _command(item.get("command"), f"preflight[{index}].command")
            message = str(item.get("unavailable_message", "")).strip()
            if not message:
                raise ValueError(
                    f"validation.preflight[{index}].unavailable_message is required"
                )
            preflight.append(ValidationProbe(command, message))

        test_groups: list[TestGroup] = []
        names: set[str] = set()
        for index, item in enumerate(
            _sequence(value.get("test_groups", ()), "test_groups")
        ):
            if not isinstance(item, Mapping):
                raise ValueError(f"validation.test_groups[{index}] must be an object")
            name = str(item.get("name", "")).strip()
            if not name or name in names:
                raise ValueError(
                    f"validation.test_groups[{index}].name must be unique and non-empty"
                )
            names.add(name)
            root = _safe_relative_path(
                item.get("root"), f"test_groups[{index}].root"
            )
            path_base = _safe_relative_path(
                item.get("path_base", "."),
                f"test_groups[{index}].path_base",
                allow_dot=True,
            )
            try:
                root.relative_to(path_base)
            except ValueError as exc:
                raise ValueError(
                    f"validation.test_groups[{index}].path_base must contain root"
                ) from exc
            suffixes = tuple(
                str(suffix)
                for suffix in _sequence(
                    item.get("suffixes", ()), f"test_groups[{index}].suffixes"
                )
                if str(suffix)
            )
            if not suffixes:
                raise ValueError(
                    f"validation.test_groups[{index}].suffixes must not be empty"
                )
            command = _command(
                item.get("command"),
                f"test_groups[{index}].command",
                require_tests_placeholder=True,
            )
            excluded = tuple(
                str(name)
                for name in _sequence(
                    item.get(
                        "exclude_directories", DEFAULT_EXCLUDED_DIRECTORIES
                    ),
                    f"test_groups[{index}].exclude_directories",
                )
                if str(name)
            )
            test_groups.append(
                TestGroup(
                    name=name,
                    root=root,
                    path_base=path_base,
                    suffixes=suffixes,
                    command=command,
                    exclude_directories=excluded,
                )
            )

        full_commands = tuple(
            _command(item, f"full_commands[{index}]")
            for index, item in enumerate(
                _sequence(value.get("full_commands", ()), "full_commands")
            )
        )
        if not full_commands:
            raise ValueError("validation.full_commands must not be empty")

        return cls(
            required_paths=required_paths,
            preflight=tuple(preflight),
            test_groups=tuple(test_groups),
            full_commands=full_commands,
            dependency_paths=dependency_paths,
        )


def default_web_validation_profile() -> ValidationProfile:
    """Backward-compatible web profile using the standalone control environment."""

    return ValidationProfile(
        required_paths=(
            Path("backend/tests"),
            Path("frontend/package.json"),
            Path("frontend/node_modules/.bin/vitest"),
            Path("frontend/node_modules/.bin/vue-tsc"),
            Path("frontend/node_modules/.bin/vite"),
        ),
        preflight=(
            ValidationProbe(
                (
                    "conda",
                    "run",
                    "-n",
                    "loop-engineering",
                    "python",
                    "-c",
                    "import pytest",
                ),
                "Conda environment 'loop-engineering' or pytest is unavailable",
            ),
            ValidationProbe(
                ("npm", "--version"),
                "Node/npm runtime is unavailable",
            ),
        ),
        test_groups=(
            TestGroup(
                name="backend-python",
                root=Path("backend/tests"),
                path_base=Path("."),
                suffixes=(".py",),
                command=(
                    "conda",
                    "run",
                    "-n",
                    "loop-engineering",
                    "pytest",
                    "-q",
                    TESTS_PLACEHOLDER,
                ),
            ),
            TestGroup(
                name="frontend-typescript",
                root=Path("frontend"),
                path_base=Path("frontend"),
                suffixes=(".test.ts",),
                command=(
                    "npm",
                    "--prefix",
                    "frontend",
                    "test",
                    "--",
                    TESTS_PLACEHOLDER,
                ),
            ),
        ),
        full_commands=(
            (
                "conda",
                "run",
                "-n",
                "loop-engineering",
                "pytest",
                "-q",
                "backend/tests",
            ),
            ("npm", "--prefix", "frontend", "test"),
            ("npm", "--prefix", "frontend", "run", "build"),
        ),
        dependency_paths=(Path("frontend/node_modules"),),
    )


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"validation.{label} must be a list")
    return value


def _command(
    value: Any,
    label: str,
    *,
    require_tests_placeholder: bool = False,
) -> tuple[str, ...]:
    parts = tuple(str(part) for part in _sequence(value, label))
    if not parts or any(not part for part in parts):
        raise ValueError(f"validation.{label} must contain non-empty arguments")
    placeholder_count = parts.count(TESTS_PLACEHOLDER)
    if require_tests_placeholder and placeholder_count != 1:
        raise ValueError(
            f"validation.{label} must contain exactly one {TESTS_PLACEHOLDER} argument"
        )
    if not require_tests_placeholder and placeholder_count:
        raise ValueError(f"validation.{label} cannot contain {TESTS_PLACEHOLDER}")
    return parts


def _safe_relative_path(
    value: Any,
    label: str,
    *,
    allow_dot: bool = False,
) -> Path:
    text = str(value if value is not None else "").strip()
    if allow_dot and text == ".":
        return Path(".")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"validation.{label} must be a safe relative path")
    return path


__all__ = [
    "DEFAULT_EXCLUDED_DIRECTORIES",
    "TESTS_PLACEHOLDER",
    "TestGroup",
    "ValidationProbe",
    "ValidationProfile",
    "default_web_validation_profile",
]
