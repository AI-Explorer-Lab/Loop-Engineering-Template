"""Render Codex prompts and persist complete human/machine final reports."""

from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Any, Mapping

from .models import ReviewRecord, RunResult, RunState, TaskSpec, ValidationRound
from .state import StateStore, redact_sensitive_text, sanitize_for_codex


_PLACEHOLDER = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- 无"


def _indented(text: str) -> str:
    if not text.strip():
        return "    （无）"
    return "\n".join(f"    {line}" for line in text.splitlines())


def _queue_context(task: TaskSpec) -> str:
    if task.queue_id is None:
        return "单任务"
    return f"长任务 {task.queue_id}，第 {task.sequence} 个子任务"


class TemplateRenderer:
    """Strict renderer for the three version-controlled Markdown templates."""

    ALLOWED_TEMPLATES = {
        "initial_prompt.md",
        "repair_prompt.md",
        "review_repair_prompt.md",
        "evaluation_repair_prompt.md",
        "final_report.md",
    }

    def __init__(self, template_dir: str | Path | None = None) -> None:
        self.template_dir = (
            Path(template_dir)
            if template_dir is not None
            else Path(__file__).with_name("templates")
        )

    def render(self, template_name: str, values: Mapping[str, object]) -> str:
        if template_name not in self.ALLOWED_TEMPLATES:
            raise ValueError(f"unsupported template: {template_name}")
        template = (self.template_dir / template_name).read_text(encoding="utf-8")
        missing = sorted(set(_PLACEHOLDER.findall(template)) - set(values))
        if missing:
            raise ValueError(f"missing template values: {', '.join(missing)}")
        rendered = _PLACEHOLDER.sub(
            lambda match: str(values[match.group(1)]), template
        )
        return redact_sensitive_text(rendered)


class PromptRenderer:
    """Build the initial and repair messages sent to the same Codex thread."""

    def __init__(
        self,
        renderer: TemplateRenderer | None = None,
        *,
        repair_summary_limit: int = 8_000,
    ) -> None:
        self.renderer = renderer or TemplateRenderer()
        self.repair_summary_limit = repair_summary_limit

    def initial_prompt(self, task: TaskSpec, state: RunState) -> str:
        return self.renderer.render(
            "initial_prompt.md",
            {
                "task_id": task.task_id,
                "queue_context": _queue_context(task),
                "requirement": task.requirement,
                "acceptance_criteria": _bullets(task.acceptance_criteria),
                "worktree": state.worktree_relative_path,
                "base_commit": state.base_commit,
                "task_branch": state.task_branch,
            },
        )

    def repair_prompt(
        self,
        task: TaskSpec,
        state: RunState,
        validation_round: ValidationRound,
        *,
        changed_files: list[str] | None = None,
        diff_sha256: str = "",
    ) -> str:
        details = self._failure_details(validation_round)
        return self.renderer.render(
            "repair_prompt.md",
            {
                "task_id": task.task_id,
                "queue_context": _queue_context(task),
                "turn_number": state.turn_count + 1,
                "requirement": task.requirement,
                "redacted_failure_summary": sanitize_for_codex(
                    details, self.repair_summary_limit
                ),
                "changed_files": ", ".join(changed_files or []) or "无",
                "diff_sha256": diff_sha256 or "尚未生成",
            },
        )

    def review_repair_prompt(
        self,
        task: TaskSpec,
        state: RunState,
        review_comment: str,
        *,
        changed_files: list[str] | None = None,
        diff_sha256: str = "",
    ) -> str:
        return self.renderer.render(
            "review_repair_prompt.md",
            {
                "task_id": task.task_id,
                "queue_context": _queue_context(task),
                "turn_number": state.turn_count + 1,
                "requirement": task.requirement,
                "review_comment": sanitize_for_codex(
                    review_comment or "审查人要求继续修改。",
                    self.repair_summary_limit,
                ),
                "changed_files": ", ".join(changed_files or []) or "无",
                "diff_sha256": diff_sha256 or "尚未生成",
            },
        )

    def evaluation_repair_prompt(
        self,
        task: TaskSpec,
        state: RunState,
        evaluation_summary: str,
        *,
        changed_files: list[str] | None = None,
        diff_sha256: str = "",
    ) -> str:
        return self.renderer.render(
            "evaluation_repair_prompt.md",
            {
                "task_id": task.task_id,
                "queue_context": _queue_context(task),
                "turn_number": state.turn_count + 1,
                "requirement": task.requirement,
                "evaluation_summary": sanitize_for_codex(
                    evaluation_summary, self.repair_summary_limit
                ),
                "changed_files": ", ".join(changed_files or []) or "无",
                "diff_sha256": diff_sha256 or "尚未生成",
            },
        )

    @staticmethod
    def _failure_details(validation_round: ValidationRound) -> str:
        parts: list[str] = []
        if validation_round.failure_summary:
            parts.append(validation_round.failure_summary)
        if validation_round.infrastructure_error:
            parts.append(f"基础设施错误：{validation_round.infrastructure_error}")
        for index, result in enumerate(validation_round.failed_results, start=1):
            output = result.stderr.strip() or result.stdout.strip() or "（无输出）"
            parts.extend(
                [
                    f"### 失败命令 {index}",
                    f"- 阶段：{result.stage or validation_round.stage}",
                    f"- 命令：`{shlex.join(result.command)}`",
                    f"- 退出码：{result.exit_code}",
                    f"- 超时：{'是' if result.timed_out else '否'}",
                    f"- 本地日志：{result.log_path or '尚未写入'}",
                    "- 错误输出：",
                    output,
                ]
            )
        return "\n".join(parts) or "验证未通过，但没有可用的错误输出。"


class ReportBuilder:
    """Create and persist the final ``result.json`` and ``report.md`` pair."""

    def __init__(self, renderer: TemplateRenderer | None = None) -> None:
        self.renderer = renderer or TemplateRenderer()

    def build(
        self,
        task: TaskSpec,
        state: RunState,
        *,
        permissions: Mapping[str, Any] | None = None,
        changes: Mapping[str, Any] | None = None,
        review: ReviewRecord | None = None,
        denied_event_count: int = 0,
    ) -> tuple[RunResult, str]:
        result = RunResult.from_run(task, state)
        if permissions is not None:
            result.permissions = dict(permissions)
        report = self.render(
            result,
            changes=changes,
            review=review,
            denied_event_count=denied_event_count,
        )
        return result, report

    def persist(
        self, store: StateStore, task: TaskSpec, state: RunState
    ) -> tuple[Path, Path]:
        result, report = self.build(task, state)
        return store.save_result(result), store.save_report(task.task_id, report)

    def render(
        self,
        result: RunResult,
        *,
        changes: Mapping[str, Any] | None = None,
        review: ReviewRecord | None = None,
        denied_event_count: int = 0,
    ) -> str:
        change_data = dict(changes or {})
        changed_file_rows = change_data.get("files", [])
        changed_files = [
            str(item.get("path"))
            for item in changed_file_rows
            if isinstance(item, Mapping) and item.get("path")
        ]
        final_diff = change_data.get("final_diff", {})
        if not isinstance(final_diff, Mapping):
            final_diff = {}
        effective = result.permissions.get("effective", {})
        requested = result.permissions.get("requested", {})
        if not isinstance(effective, Mapping):
            effective = {}
        if not isinstance(requested, Mapping):
            requested = {}
        values = {
            "task_id": result.task_id,
            "queue_context": (
                "单任务"
                if result.queue_id is None
                else f"长任务 {result.queue_id}，第 {result.sequence} 个子任务"
            ),
            "thread_id": result.thread_id or "未创建",
            "turn_count": result.turn_count,
            "requirement": result.requirement,
            "acceptance_criteria": _bullets(result.acceptance_criteria),
            "base_ref": result.workspace.get("base_ref", "未知"),
            "base_commit": result.workspace.get("base_commit", "未知"),
            "task_branch": result.workspace.get("task_branch", "未知"),
            "worktree": result.workspace.get("worktree", "未知"),
            "filesystem_summary": (
                "只允许任务 worktree；Git 元数据和运行依赖只读"
                if effective.get("verified")
                else "未验证"
            ),
            "network_status": effective.get(
                "network", requested.get("network", "未知")
            ),
            "approval_mode": effective.get(
                "approval_mode", requested.get("approval_mode", "未知")
            ),
            "denied_event_count": denied_event_count,
            "artifact_links": "turns/、events.jsonl、logs/codex/",
            "validation_rounds": self._rounds(result),
            "changed_files": ", ".join(changed_files) or "无",
            "diff_path": final_diff.get("path", "changes/final.diff"),
            "diff_sha256": final_diff.get(
                "raw_sha256", result.final_diff_sha256 or "尚未生成"
            ),
            "redaction_count": final_diff.get(
                "redaction_count", result.diff_redaction_count
            ),
            "review_status": result.review_status.value,
            "reviewer": review.reviewer if review else "待填写",
            "decision": review.decision.value if review else "待填写",
            "comment": review.comment if review else "待填写",
            "reviewed_diff_sha256": (
                review.reviewed_diff_sha256 if review else "待填写"
            ),
        }
        return self.renderer.render("final_report.md", values)

    @staticmethod
    def _rounds(result: RunResult) -> str:
        if not result.rounds:
            return "尚未执行验证。"
        sections: list[str] = []
        for validation_round in result.rounds:
            sections.append(
                f"### 第 {validation_round.round_number} 轮 — "
                f"{'通过' if validation_round.passed else '失败'}"
            )
            sections.append(f"- 结束阶段：{validation_round.stage}")
            sections.append(
                f"- 时间：{validation_round.started_at} → "
                f"{validation_round.finished_at or '未完成'}"
            )
            command_results = validation_round.command_results
            if not command_results:
                sections.append("- 命令：无")
                continue
            for command in command_results:
                outcome = "通过" if command.passed else "失败"
                sections.append(
                    f"- `{shlex.join(command.command)}`：{outcome}，"
                    f"退出码 {command.exit_code}，耗时 "
                    f"{command.duration_seconds:.3f}s，日志 "
                    f"`{command.log_path or '未写入'}`"
                )
        return "\n".join(sections)

    @staticmethod
    def _failures(result: RunResult) -> str:
        items: list[str] = []
        if result.infrastructure_error:
            items.append(f"- 基础设施故障：{result.infrastructure_error}")
        for validation_round in result.rounds:
            if validation_round.passed:
                continue
            summary = validation_round.failure_summary.strip() or "验证未通过"
            items.append(f"- 第 {validation_round.round_number} 轮：{summary}")
            for command in validation_round.failed_results:
                items.append(
                    f"  - `{shlex.join(command.command)}`，退出码 "
                    f"{command.exit_code}，日志 `{command.log_path or '未写入'}`"
                )
        return "\n".join(items) or "无。"


def render_initial_prompt(task: TaskSpec, state: RunState) -> str:
    return PromptRenderer().initial_prompt(task, state)


def render_repair_prompt(
    task: TaskSpec,
    state: RunState,
    validation_round: ValidationRound,
    *,
    max_chars: int = 8_000,
) -> str:
    return PromptRenderer(repair_summary_limit=max_chars).repair_prompt(
        task, state, validation_round
    )


__all__ = [
    "PromptRenderer",
    "ReportBuilder",
    "TemplateRenderer",
    "render_initial_prompt",
    "render_repair_prompt",
]
