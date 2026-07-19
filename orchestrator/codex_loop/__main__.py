"""Command-line entry point for single tasks and ordered task queues."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .models import (
    DeliveryStatus,
    InfrastructureError,
    QueueStatus,
    ReviewStatus,
    RunResult,
    RunState,
    RunStatus,
    TaskSpec,
)
from .git_delivery import GitDeliveryService
from .harness_runtime import HarnessRuntime
from .report import ReportBuilder
from .queue_workflow import QueueWorkflow
from .review import ReviewError, ReviewService
from .state import ActiveRunError, QueueStore, StateStore, redact_sensitive_text
from .workflow import OrchestrationWorkflow


REPO_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.codex_loop",
        description="Run one task or an ordered task queue through Codex and validation.",
    )
    parser.add_argument(
        "--project-id",
        help="managed project id from orchestrator backend configuration",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="start one new task")
    start_parser.add_argument("--task-file", type=Path)
    start_parser.add_argument("--task-id")
    start_parser.add_argument("--requirement")
    start_parser.add_argument(
        "--acceptance-criterion",
        action="append",
        dest="acceptance_criteria",
        help="repeat once for each acceptance criterion",
    )
    _add_timeout_argument(start_parser)

    resume_parser = subparsers.add_parser("resume", help="resume a saved task")
    resume_parser.add_argument("--task-id")
    _add_timeout_argument(resume_parser)

    review_parser = subparsers.add_parser(
        "review", help="record a human review for a task or queue subtask"
    )
    review_parser.add_argument("--task-id", required=True)
    review_parser.add_argument(
        "--decision",
        required=True,
        choices=[
            ReviewStatus.APPROVED.value,
            ReviewStatus.CHANGES_REQUESTED.value,
            ReviewStatus.REJECTED.value,
        ],
    )
    review_parser.add_argument("--reviewer", required=True)
    review_parser.add_argument("--comment", default="")
    review_parser.add_argument("--reviewed-diff-sha256", required=True)
    review_parser.add_argument("--commit-subject", default="")

    show_parser = subparsers.add_parser(
        "show", help="show read-only workspace, permission, and audit metadata"
    )
    show_parser.add_argument("--task-id", required=True)

    queue_start_parser = subparsers.add_parser(
        "queue-start", help="start one manually split ordered task queue"
    )
    queue_start_parser.add_argument("--task-file", type=Path, required=True)
    _add_timeout_argument(queue_start_parser)

    queue_resume_parser = subparsers.add_parser(
        "queue-resume", help="resume a queue stopped by an infrastructure error"
    )
    queue_resume_parser.add_argument("--queue-id")
    _add_timeout_argument(queue_resume_parser)

    queue_show_parser = subparsers.add_parser(
        "queue-show", help="show ordered queue progress"
    )
    queue_show_parser.add_argument("--queue-id", required=True)
    return parser


def _add_timeout_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=900.0,
        help="timeout for each validation command (default: 900)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        project = _configured_project(args.project_id)
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            f"编排器配置错误：{redact_sensitive_text(str(exc))}",
            file=sys.stderr,
        )
        return 2
    project_root = Path(project["repo_root"])
    validation_profile = project["validation_profile"]
    store = StateStore(project_root)
    queue_store = QueueStore(project_root)

    try:
        if args.command == "start":
            try:
                task = _task_from_arguments(args)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                result = _record_invalid_configuration(store, exc)
                _print_result(result, store)
                return 2
            runtime = _configured_harness_runtime(project, args.timeout_seconds)
            workflow = (
                runtime.workflow(store=store)
                if runtime is not None
                else OrchestrationWorkflow(
                    project_root,
                    store=store,
                    validation_timeout_seconds=args.timeout_seconds,
                    validation_profile=validation_profile,
                )
            )
            result = workflow.start(task)
        elif args.command == "resume":
            task_id = args.task_id or _find_resumable_task_id(store)
            runtime = _configured_harness_runtime(project, args.timeout_seconds)
            workflow = (
                runtime.workflow(store=store)
                if runtime is not None
                else OrchestrationWorkflow(
                    project_root,
                    store=store,
                    validation_timeout_seconds=args.timeout_seconds,
                    validation_profile=validation_profile,
                )
            )
            result = workflow.resume(task_id)
        elif args.command == "review":
            runtime = _configured_harness_runtime(project)
            queue_id = queue_store.find_queue_for_task(args.task_id)
            if queue_id is None:
                review = ReviewService(project_root, store=store).record(
                    args.task_id,
                    decision=args.decision,
                    reviewer=args.reviewer,
                    comment=args.comment,
                    reviewed_diff_sha256=args.reviewed_diff_sha256,
                    commit_subject=args.commit_subject,
                )
                payload: dict[str, Any] = {"review": review.to_dict()}
                if review.decision is ReviewStatus.APPROVED and runtime is not None:
                    payload["commit"] = GitDeliveryService(
                        project_root, store=store
                    ).deliver(args.task_id, review=review)
                    payload["archive"] = _archive_after_commit(
                        runtime, args.task_id, store
                    )
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                workflow = (
                    runtime.queue_workflow()
                    if runtime is not None
                    else QueueWorkflow(
                        project_root,
                        queue_store=queue_store,
                        validation_profile=validation_profile,
                    )
                )
                queue_state, review = workflow.record_review(
                    queue_id,
                    args.task_id,
                    decision=args.decision,
                    reviewer=args.reviewer,
                    comment=args.comment,
                    reviewed_diff_sha256=args.reviewed_diff_sha256,
                    commit_subject=args.commit_subject,
                )
                if queue_state.status in {QueueStatus.PENDING, QueueStatus.RUNNING}:
                    queue_state = workflow.run_current(queue_id)
                print(
                    json.dumps(
                        {"review": review.to_dict(), "queue": queue_state.to_dict()},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return 0
        elif args.command == "show":
            _print_run_metadata(store, args.task_id)
            return 0
        elif args.command == "queue-start":
            values = _queue_values_from_file(args.task_file)
            runtime = _configured_harness_runtime(project, args.timeout_seconds)
            workflow = (
                runtime.queue_workflow()
                if runtime is not None
                else QueueWorkflow(
                    project_root,
                    queue_store=queue_store,
                    validation_timeout_seconds=args.timeout_seconds,
                    validation_profile=validation_profile,
                )
            )
            queue_state = workflow.start(
                values["name"],
                values["subtasks"],
                queue_id=values.get("queue_id"),
                base_ref=values.get("base_ref", "HEAD"),
            )
            _print_queue(queue_store, queue_state.queue_id)
            return 0 if queue_state.status is QueueStatus.COMPLETED else 1
        elif args.command == "queue-resume":
            queue_id = args.queue_id or _find_resumable_queue_id(queue_store)
            runtime = _configured_harness_runtime(project, args.timeout_seconds)
            workflow = (
                runtime.queue_workflow()
                if runtime is not None
                else QueueWorkflow(
                    project_root,
                    queue_store=queue_store,
                    validation_timeout_seconds=args.timeout_seconds,
                    validation_profile=validation_profile,
                )
            )
            queue_state = workflow.resume(queue_id)
            _print_queue(queue_store, queue_id)
            return 0 if queue_state.status is QueueStatus.COMPLETED else 1
        else:
            _print_queue(queue_store, args.queue_id)
            return 0
    except ActiveRunError as exc:
        print(f"无法启动：{redact_sensitive_text(str(exc))}", file=sys.stderr)
        return 2
    except (InfrastructureError, ReviewError, OSError, ValueError) as exc:
        print(f"编排器错误：{redact_sensitive_text(str(exc))}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("已取消。", file=sys.stderr)
        return 130

    _print_result(result, store)
    if result.status is RunStatus.SUCCESS:
        return 0
    if result.status is RunStatus.MANUAL_REVIEW:
        return 1
    return 2


def _configured_project(project_id: str | None = None) -> dict[str, object]:
    from orchestrator.backend.config.config import projects_from_settings, settings

    projects = projects_from_settings(settings)
    if project_id:
        try:
            return next(
                item for item in projects if item["project_id"] == project_id
            )
        except StopIteration as exc:
            raise ValueError(f"unknown configured project: {project_id}") from exc
    return next(item for item in projects if bool(item["is_default"]))


def _configured_harness_runtime(
    project: dict[str, object],
    timeout_seconds: float | None = None,
) -> HarnessRuntime | None:
    """Build the same project-scoped Harness runtime used by the local API."""

    from orchestrator.backend.config.config import knowledge_from_settings, settings

    agent = settings.get("agent", {}) or {}
    if not bool(agent.get("harness_enabled", False)):
        return None
    knowledge = knowledge_from_settings(settings)
    return HarnessRuntime(
        Path(project["repo_root"]),
        project_id=str(project["project_id"]),
        knowledge_actor_id=str(project.get("knowledge_actor_id", "")),
        knowledge_writer_actor_id=str(
            knowledge.get("knowledge_writer_actor_id", "")
        ),
        mcp_registry=str(knowledge.get("mcp_registry", "")),
        validation_timeout_seconds=float(
            timeout_seconds
            if timeout_seconds is not None
            else agent.get("validation_timeout_seconds", 900)
        ),
        validation_profile=project["validation_profile"],
    )


def _archive_after_commit(
    runtime: HarnessRuntime,
    task_id: str,
    store: StateStore,
) -> dict[str, Any]:
    """Keep a successful task commit when local archive/knowledge work fails."""

    try:
        return runtime.archive(task_id, store=store)
    except Exception as exc:
        message = redact_sensitive_text(str(exc) or type(exc).__name__)
        state = store.load_state(task_id)
        state.delivery_status = DeliveryStatus.FAILED
        state.last_error_summary = message
        store.save_state(state)
        store.append_event(
            task_id,
            "knowledge.write_failed",
            {"error": message},
        )
        return {"status": "failed", "error": message}


def _task_from_arguments(args: argparse.Namespace) -> TaskSpec:
    if args.task_file is not None:
        if args.requirement or args.acceptance_criteria or args.task_id:
            raise ValueError(
                "--task-file cannot be combined with requirement, criteria, or task ID"
            )
        return TaskSpec.from_file(args.task_file)

    requirement = args.requirement
    criteria = list(args.acceptance_criteria or [])
    if requirement is None and not criteria:
        if not sys.stdin.isatty():
            raise ValueError(
                "provide --task-file or requirement and acceptance criteria"
            )
        requirement, criteria = _interactive_task_input()
    elif requirement is None or not criteria:
        raise ValueError(
            "requirement and at least one acceptance criterion must be provided together"
        )

    values: dict[str, Any] = {
        "requirement": requirement,
        "acceptance_criteria": criteria,
    }
    if args.task_id:
        values["task_id"] = args.task_id
    return TaskSpec.from_dict(values)


def _interactive_task_input() -> tuple[str, list[str]]:
    requirement = input("功能需求：").strip()
    first = input("验收标准 1：").strip()
    criteria = [first] if first else []
    index = 2
    while criteria:
        criterion = input(f"验收标准 {index}（直接回车结束）：").strip()
        if not criterion:
            break
        criteria.append(criterion)
        index += 1
    return requirement, criteria


def _queue_values_from_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("queue task file must contain a JSON object")
    name = str(data.get("name", "")).strip()
    subtasks = data.get("subtasks")
    if not name:
        raise ValueError("queue name cannot be blank")
    if not isinstance(subtasks, list):
        raise ValueError("queue subtasks must be a JSON array")
    return {
        "name": name,
        "subtasks": subtasks,
        "queue_id": data.get("queue_id"),
        "base_ref": str(data.get("base_ref", "HEAD")),
    }


def _find_resumable_task_id(store: StateStore) -> str:
    candidates: list[RunState] = []
    if store.runs_root.is_dir():
        for state_path in store.runs_root.glob("*/state.json"):
            try:
                state = store.load_state(state_path.parent.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not state.status.is_final:
                candidates.append(state)

    if not candidates:
        raise ValueError("no unfinished task is available to resume")
    if len(candidates) > 1:
        task_ids = ", ".join(sorted(state.task_id for state in candidates))
        raise ValueError(f"multiple unfinished tasks found; choose --task-id: {task_ids}")
    return candidates[0].task_id


def _find_resumable_queue_id(store: QueueStore) -> str:
    candidates = store.unfinished_queue_ids()
    if not candidates:
        raise ValueError("no unfinished queue is available to resume")
    if len(candidates) > 1:
        raise ValueError(
            "multiple unfinished queues found; choose --queue-id: "
            + ", ".join(candidates)
        )
    return candidates[0]


def _record_invalid_configuration(
    store: StateStore, error: Exception
) -> RunResult:
    task = TaskSpec(
        requirement="任务配置无效，未向 Codex 发送需求",
        acceptance_criteria=["人工修正任务输入后重新启动"],
    )
    lock = store.acquire_active_lock(task.task_id)
    try:
        unfinished = store.unfinished_task_ids()
        if unfinished:
            raise ActiveRunError(
                "an unfinished task must be resumed or reviewed before recording "
                f"another task (task_id={', '.join(unfinished)})"
            )
        state = store.initialize_run(task)
        message = (
            "Invalid task configuration "
            f"({type(error).__name__}): {redact_sensitive_text(str(error))}"
        )
        state.mark_infrastructure_error(message)
        store.save_state(state)
        result, report = ReportBuilder().build(task, state)
        store.save_result(result)
        store.save_report(task.task_id, report)
        return result
    finally:
        store.release_active_lock(lock)


def _print_result(result: RunResult, store: StateStore) -> None:
    run_dir = store.run_dir(result.task_id)
    print(f"任务：{result.task_id}")
    print(f"状态：{result.status.value}")
    print(f"报告：{run_dir / 'report.md'}")
    print(f"结果：{run_dir / 'result.json'}")


def _print_run_metadata(store: StateStore, task_id: str) -> None:
    run_dir = store.run_dir(task_id)
    state = store.load_state(task_id)
    data: dict[str, Any] = {
        "task_id": task_id,
        "schema_version": state.schema_version,
        "machine_status": state.status.value,
        "review_status": state.review_status.value,
        "workspace": None,
        "permissions": None,
        "audit": {"event_count": 0, "denied_event_count": 0},
    }
    manifest_path = run_dir / "manifest.json"
    permissions_path = run_dir / "permissions.json"
    events_path = run_dir / "events.jsonl"
    if manifest_path.is_file():
        data["workspace"] = json.loads(manifest_path.read_text(encoding="utf-8"))
    if permissions_path.is_file():
        data["permissions"] = json.loads(
            permissions_path.read_text(encoding="utf-8")
        )
    if events_path.is_file():
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        data["audit"] = {
            "event_count": len(events),
            "denied_event_count": sum(
                event.get("type") == "permission.denied" for event in events
            ),
        }
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_queue(store: QueueStore, queue_id: str) -> None:
    spec = store.load_spec(queue_id)
    state = store.load_state(queue_id)
    print(
        json.dumps(
            {
                "queue": spec.to_dict(),
                "state": state.to_dict(),
                "report": str(store.queue_dir(queue_id) / "report.md"),
                "cumulative_diff": str(store.cumulative_diff_path(queue_id)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
