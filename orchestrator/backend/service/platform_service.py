from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import smtplib
import tempfile
from threading import RLock
from typing import Any, Iterable
from urllib.request import Request, urlopen

from orchestrator.codex_loop.state import QueueStore, StateStore, redact_sensitive_text

from ..exceptions.business_exception import BusinessException
from ..mapper.file_queue import FileQueueMapper
from ..mapper.file_run import FileRunMapper
from .project_registry import ProjectContext, ProjectRegistry


@dataclass(frozen=True, slots=True)
class HistoryItem:
    kind: str
    identifier: str
    project_id: str
    project_name: str
    title: str
    status: str
    review_status: str | None
    started_at: str
    updated_at: str
    finished_at: str | None
    current_task_id: str | None = None
    delivery_status: str | None = None


class PlatformService:
    """Cross-project read models, artifact access, and local notifications."""

    def __init__(self, registry: ProjectRegistry, config: Any) -> None:
        self.registry = registry
        self.notification_config = config.get("notifications", {}) or {}
        self._notification_lock = RLock()

    def projects(self) -> list[dict[str, Any]]:
        return [
            {
                "project_id": context.project_id,
                "name": context.name,
                "repo_root": str(context.repo_root),
                "is_default": context.is_default,
                "active_identifier": context.task_service.executor.active_task_id(),
                "knowledge_actor_id": context.knowledge_actor_id,
            }
            for context in self.registry.all()
        ]

    def history(
        self,
        *,
        project_id: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        contexts = (
            [self.registry.get(project_id)]
            if project_id
            else self.registry.all()
        )
        items: list[HistoryItem] = []
        for context in contexts:
            items.extend(self._project_history(context))
        if kind:
            items = [item for item in items if item.kind == kind]
        if status:
            items = [item for item in items if item.status == status]
        normalized_query = query.strip().casefold()
        if normalized_query:
            items = [
                item
                for item in items
                if normalized_query
                in " ".join(
                    [item.identifier, item.title, item.project_name, item.status]
                ).casefold()
            ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return {
            "items": [asdict(item) for item in items[start : start + page_size]],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size if total else 0,
        }

    def events(
        self,
        project_id: str,
        kind: str,
        identifier: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        context = self.registry.get(project_id)
        root = self._artifact_root(context, kind, identifier)
        path = root / "events.jsonl"
        events: list[dict[str, Any]] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict) or int(value.get("seq", 0)) <= after:
                    continue
                events.append(value)
                if len(events) >= limit:
                    break
        next_cursor = int(events[-1].get("seq", after)) if events else after
        return {
            "items": events,
            "next_cursor": next_cursor,
            "terminal": self._is_terminal(context, kind, identifier),
        }

    def logs(
        self,
        project_id: str,
        kind: str,
        identifier: str,
    ) -> list[dict[str, Any]]:
        context = self.registry.get(project_id)
        root = self._artifact_root(context, kind, identifier)
        values: list[dict[str, Any]] = []
        for path in sorted(root.glob("**/logs/**/*.log")):
            relative = path.relative_to(root).as_posix()
            values.append(
                {
                    "log_id": relative,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        return values

    def read_log(
        self,
        project_id: str,
        kind: str,
        identifier: str,
        log_id: str,
    ) -> str:
        context = self.registry.get(project_id)
        root = self._artifact_root(context, kind, identifier)
        candidate = (root / log_id).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            raise BusinessException("Invalid log path", status_code=400) from None
        if candidate.suffix != ".log" or not candidate.is_file():
            raise BusinessException("Log not found", status_code=404)
        return candidate.read_text(encoding="utf-8")

    def notifications(
        self,
        *,
        project_id: str | None = None,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        contexts = (
            [self.registry.get(project_id)]
            if project_id
            else self.registry.all()
        )
        values: list[dict[str, Any]] = []
        for context in contexts:
            self._sync_notifications(context)
            values.extend(self._read_notifications(context))
        if unread_only:
            values = [item for item in values if item.get("read_at") is None]
        values.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return values

    def mark_notification_read(
        self,
        project_id: str,
        notification_id: str,
    ) -> dict[str, Any]:
        context = self.registry.get(project_id)
        with self._notification_lock:
            values = self._read_notifications(context)
            target: dict[str, Any] | None = None
            for item in values:
                if item.get("notification_id") == notification_id:
                    item["read_at"] = datetime.now().astimezone().isoformat(
                        timespec="milliseconds"
                    )
                    target = item
                    break
            if target is None:
                raise BusinessException("Notification not found", status_code=404)
            self._write_notifications(context, values)
            return target

    def notification_settings(self) -> dict[str, bool]:
        return self.notification_settings_for(self.registry.default.project_id)

    def notification_settings_for(self, project_id: str) -> dict[str, bool]:
        context = self.registry.get(project_id)
        preferences = self._read_notification_settings(context)
        return {
            "in_app": bool(preferences.get("in_app", True)),
            "browser": bool(preferences.get("browser", True)),
            "email_configured": bool(
                self.notification_config.get("smtp_host")
                and self.notification_config.get("smtp_sender")
                and self.notification_config.get("smtp_recipient")
            ),
            "webhook_configured": bool(
                self.notification_config.get("webhook_url")
            ),
        }

    def update_notification_settings(
        self,
        project_id: str,
        *,
        in_app: bool,
        browser: bool,
    ) -> dict[str, bool]:
        context = self.registry.get(project_id)
        self._write_json(
            self._notification_settings_path(context),
            {"in_app": bool(in_app), "browser": bool(browser)},
        )
        return self.notification_settings_for(project_id)

    def _project_history(self, context: ProjectContext) -> Iterable[HistoryItem]:
        run_mapper = FileRunMapper(context.repo_root)
        if run_mapper.store.runs_root.is_dir():
            for task_path in run_mapper.store.runs_root.glob("*/task.json"):
                task_id = task_path.parent.name
                try:
                    snapshot = run_mapper.load_snapshot(task_id)
                    task = run_mapper.load_task(task_id)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if snapshot is None or task is None:
                    continue
                yield HistoryItem(
                    kind="task",
                    identifier=task_id,
                    project_id=context.project_id,
                    project_name=context.name,
                    title=task.requirement,
                    status=snapshot.status,
                    review_status=snapshot.review_status,
                    started_at=snapshot.started_at,
                    updated_at=snapshot.updated_at,
                    finished_at=snapshot.finished_at,
                    delivery_status=snapshot.delivery_status,
                )
        queue_mapper = FileQueueMapper(context.repo_root)
        if queue_mapper.store.queues_root.is_dir():
            for queue_path in queue_mapper.store.queues_root.glob("*/queue.json"):
                queue_id = queue_path.parent.name
                try:
                    snapshot = queue_mapper.load_snapshot(queue_id)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if snapshot is None:
                    continue
                yield HistoryItem(
                    kind="queue",
                    identifier=queue_id,
                    project_id=context.project_id,
                    project_name=context.name,
                    title=snapshot.name,
                    status=snapshot.status,
                    review_status=None,
                    started_at=snapshot.started_at,
                    updated_at=snapshot.updated_at,
                    finished_at=snapshot.finished_at,
                    current_task_id=snapshot.current_task_id,
                    delivery_status=snapshot.delivery_status,
                )

    def capabilities(self, project_id: str | None = None) -> dict[str, Any]:
        context = self.registry.get(project_id)
        if context.harness is None:
            return {
                "status": "unavailable",
                "project_id": context.project_id,
                "reason": "harness feature is disabled",
            }
        value = context.harness.capabilities()
        value["project_id"] = context.project_id
        value["knowledge_actor_id"] = context.knowledge_actor_id
        value["checked_at"] = datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        )
        return value

    def metrics(self, project_id: str | None = None) -> dict[str, Any]:
        context = self.registry.get(project_id)
        mapper = FileRunMapper(context.repo_root)
        completed = 0
        successes = 0
        layer_failures = {
            "syntax": 0,
            "logic": 0,
            "specification": 0,
            "architecture": 0,
        }
        repair_rounds = 0
        knowledge_tasks = 0
        planned = 0
        planner_edits = 0
        committed = 0
        commit_failed = 0
        root = context.repo_root / ".codex-orchestrator"
        run_roots = [path.parent for path in root.glob("runs/*/state.json")]
        run_roots.extend(path.parent for path in root.glob("queues/*/subtasks/*/state.json"))
        for run_dir in run_roots:
            task_id = run_dir.name
            try:
                state = StateStore(
                    context.repo_root, runs_root=run_dir.parent
                ).load_state(task_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if state.status.is_final:
                completed += 1
            if state.status.value == "success":
                successes += 1
            repair_rounds += max(0, state.turn_count - 1)
            aggregate = self._optional_json(run_dir / "evaluations/aggregate.json")
            for finding in aggregate.get("blocking_findings", []):
                layer = str(finding.get("layer", ""))
                if layer in layer_failures:
                    layer_failures[layer] += 1
            if self._optional_json(run_dir / "context/generation.json").get("knowledge"):
                knowledge_tasks += 1
            confirmation = self._optional_json(run_dir / "plan/confirmation.json")
            if confirmation:
                planned += 1
                planner_edits += int(confirmation.get("manual_edit_count", 0))
            delivery = self._optional_json(run_dir / "delivery/commit.json")
            if delivery.get("status") == "committed":
                committed += 1
            elif delivery.get("status") == "failed":
                commit_failed += 1
        backlog = (
            context.harness._archive_backlog() if context.harness is not None else 0
        )
        return {
            "project_id": context.project_id,
            "task_success_rate": successes / completed if completed else None,
            "completed_tasks": completed,
            "layer_failure_counts": layer_failures,
            "repair_rounds": repair_rounds,
            "knowledge_hit_rate": knowledge_tasks / completed if completed else None,
            "planned_tasks": planned,
            "planner_manual_edit_count": planner_edits,
            "commit_success_rate": (
                committed / (committed + commit_failed)
                if committed + commit_failed
                else None
            ),
            "archive_backlog": backlog,
        }

    def _artifact_root(
        self,
        context: ProjectContext,
        kind: str,
        identifier: str,
    ) -> Path:
        if kind == "queue":
            root = QueueStore(context.repo_root).queue_dir(identifier)
            if not (root / "queue.json").is_file():
                raise BusinessException("Queue not found", status_code=404)
            return root
        if kind != "task":
            raise BusinessException("History kind must be task or queue", status_code=400)
        store = StateStore(context.repo_root)
        root = store.run_dir(identifier)
        if (root / "task.json").is_file():
            return root
        queue_store = QueueStore(context.repo_root)
        queue_id = queue_store.find_queue_for_task(identifier)
        if queue_id is None:
            raise BusinessException("Task not found", status_code=404)
        return queue_store.subtask_store(queue_id).run_dir(identifier)

    def _is_terminal(
        self,
        context: ProjectContext,
        kind: str,
        identifier: str,
    ) -> bool:
        if kind == "queue":
            snapshot = FileQueueMapper(context.repo_root).load_snapshot(identifier)
            return bool(
                snapshot
                and snapshot.status
                in {"completed", "cancelled", "rejected", "infrastructure_error"}
            )
        root = self._artifact_root(context, kind, identifier)
        state = StateStore(
            context.repo_root,
            runs_root=root.parent,
        ).load_state(identifier)
        return state.status.value in {
            "success",
            "manual_review",
            "infrastructure_error",
            "cancelled",
        }

    def _notification_path(self, context: ProjectContext) -> Path:
        return context.repo_root / ".codex-orchestrator" / "notifications.json"

    @staticmethod
    def _optional_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _notification_settings_path(self, context: ProjectContext) -> Path:
        return (
            context.repo_root
            / ".codex-orchestrator"
            / "notification-settings.json"
        )

    def _read_notification_settings(
        self,
        context: ProjectContext,
    ) -> dict[str, bool]:
        path = self._notification_settings_path(context)
        if not path.is_file():
            return {"in_app": True, "browser": True}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"in_app": True, "browser": True}
        if not isinstance(value, dict):
            return {"in_app": True, "browser": True}
        return {
            "in_app": bool(value.get("in_app", True)),
            "browser": bool(value.get("browser", True)),
        }

    def _read_notifications(self, context: ProjectContext) -> list[dict[str, Any]]:
        path = self._notification_path(context)
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _write_notifications(
        self,
        context: ProjectContext,
        values: list[dict[str, Any]],
    ) -> None:
        self._write_json(self._notification_path(context), values)

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".notifications.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _sync_notifications(self, context: ProjectContext) -> None:
        with self._notification_lock:
            existing = self._read_notifications(context)
            known = {str(item.get("notification_id")) for item in existing}
            created: list[dict[str, Any]] = []
            for item in self._project_history(context):
                category = self._notification_category(item)
                if category is None:
                    continue
                stable = f"{item.project_id}:{item.kind}:{item.identifier}:{category}:{item.updated_at}"
                notification_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
                if notification_id in known:
                    continue
                notification = {
                    "notification_id": notification_id,
                    "project_id": item.project_id,
                    "kind": item.kind,
                    "identifier": item.identifier,
                    "category": category,
                    "title": item.title,
                    "message": self._notification_message(item, category),
                    "created_at": item.updated_at,
                    "read_at": None,
                    "delivery": {},
                }
                notification["delivery"] = self._deliver(notification)
                existing.append(notification)
                created.append(notification)
                known.add(notification_id)
            if created:
                self._write_notifications(context, existing)

    @staticmethod
    def _notification_category(item: HistoryItem) -> str | None:
        if item.status in {"infrastructure_error"}:
            return "failure"
        if item.status == "cancelled":
            return "cancelled"
        if item.kind == "queue" and item.status == "waiting_review":
            return "waiting_review"
        if item.kind == "queue" and item.status == "completed":
            return "completed"
        if item.kind == "task" and item.status in {"success", "manual_review"}:
            return "waiting_review" if item.review_status == "pending" else "completed"
        return None

    @staticmethod
    def _notification_message(item: HistoryItem, category: str) -> str:
        labels = {
            "waiting_review": "等待人工审查",
            "completed": "已完成",
            "failure": "运行环境故障",
            "cancelled": "已取消",
        }
        return f"{item.title} · {labels[category]}"

    def _deliver(self, notification: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        webhook = self.notification_config.get("webhook_url")
        if webhook:
            try:
                request = Request(
                    str(webhook),
                    data=json.dumps(notification, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    result["webhook"] = f"sent:{response.status}"
            except Exception as exc:  # delivery failure must never fail a task
                result["webhook"] = f"failed:{redact_sensitive_text(type(exc).__name__)}"
        email_configured = bool(
            self.notification_config.get("smtp_host")
            and self.notification_config.get("smtp_sender")
            and self.notification_config.get("smtp_recipient")
        )
        if email_configured:
            try:
                host = str(self.notification_config["smtp_host"])
                port = int(self.notification_config.get("smtp_port", 587))
                sender = str(self.notification_config["smtp_sender"])
                recipient = str(self.notification_config["smtp_recipient"])
                body = (
                    f"From: {sender}\r\nTo: {recipient}\r\n"
                    f"Subject: Codex Orchestrator notification\r\n\r\n"
                    f"{notification['message']}"
                )
                with smtplib.SMTP(host, port, timeout=3) as client:
                    if bool(self.notification_config.get("smtp_starttls", True)):
                        client.starttls()
                    username = self.notification_config.get("smtp_username")
                    password = self.notification_config.get("smtp_password")
                    if username and password:
                        client.login(str(username), str(password))
                    client.sendmail(sender, [recipient], body.encode("utf-8"))
                result["email"] = "sent"
            except Exception as exc:  # delivery failure must never fail a task
                result["email"] = f"failed:{redact_sensitive_text(type(exc).__name__)}"
        return result
