"""Human review decisions bound to exact single-task or queue-child diffs."""

from __future__ import annotations

import json
from pathlib import Path

from .audit import AuditRecorder
from .models import (
    DeliveryStatus,
    InfrastructureError,
    ReviewRecord,
    ReviewStatus,
)
from .report import ReportBuilder
from .state import StateStore


class ReviewError(ValueError):
    """A review request that cannot be accepted without changing run history."""


class ReviewService:
    """Persist one immutable single review or append a queue review revision."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        store: StateStore | None = None,
        report_builder: ReportBuilder | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.store = store or StateStore(self.repo_root)
        self.report_builder = report_builder or ReportBuilder()

    def record(
        self,
        task_id: str,
        *,
        decision: ReviewStatus | str,
        reviewer: str,
        comment: str,
        reviewed_diff_sha256: str,
        commit_subject: str = "",
    ) -> ReviewRecord:
        """Record a decision after rechecking the live worktree diff."""

        try:
            normalized_decision = (
                decision
                if isinstance(decision, ReviewStatus)
                else ReviewStatus(str(decision))
            )
        except ValueError as exc:
            raise ReviewError(
                "decision must be approved, changes_requested, or rejected"
            ) from exc
        if normalized_decision is ReviewStatus.PENDING:
            raise ReviewError(
                "decision must be approved, changes_requested, or rejected"
            )

        lock = self.store.acquire_active_lock(task_id)
        try:
            run_dir = self.store.run_dir(task_id)
            review_path = run_dir / "review.json"
            task = self.store.load_task(task_id)
            state = self.store.load_state(task_id)
            queue_task = task.queue_id is not None
            if review_path.exists() and not queue_task:
                raise ReviewError("this task already has an immutable review")

            if state.schema_version == 0:
                raise ReviewError("legacy_v0 runs are read-only")
            if not state.status.is_final:
                raise ReviewError("the task must reach a machine terminal state first")
            if not state.base_commit:
                raise InfrastructureError("run state has no baseline commit")
            if state.diff_redaction_count:
                raise ReviewError(
                    "the final diff contains redacted sensitive information and cannot be reviewed"
                )
            if (
                queue_task
                and normalized_decision is ReviewStatus.APPROVED
                and state.status.value != "success"
            ):
                raise ReviewError(
                    "a queued subtask can only be approved after validation succeeds"
                )

            changes = self._load_changes(run_dir)
            final_diff = changes.get("final_diff", {})
            stored_raw_sha = str(final_diff.get("raw_sha256", ""))
            supplied_sha = str(reviewed_diff_sha256).strip()
            audit = AuditRecorder(
                run_dir,
                state.repo_root,
                state.base_commit,
                inherited_baseline=state.inherited_baseline,
                queue_task=queue_task,
            )
            current_sha = audit.current_diff_sha256()
            expected = state.last_diff_sha256 or stored_raw_sha
            if not expected or stored_raw_sha != expected:
                raise ReviewError("saved diff metadata is inconsistent")
            if supplied_sha != expected:
                raise ReviewError("reviewed_diff_sha256 does not match the saved diff")
            if current_sha != expected:
                raise ReviewError("the task worktree changed after the final diff was captured")

            resolved_subject = str(commit_subject).strip()
            if normalized_decision is ReviewStatus.APPROVED and not resolved_subject:
                resolved_subject = task.requirement.splitlines()[0].strip()[:200]
            review = ReviewRecord(
                task_id=task_id,
                decision=normalized_decision,
                reviewer=reviewer,
                comment=comment,
                machine_status=state.status,
                reviewed_diff_sha256=supplied_sha,
                commit_subject=resolved_subject,
            )
            if queue_task:
                review.review_number = len(self.store.load_review_history(task_id)) + 1
                self.store.archive_review_revision(task_id, review.review_number)
                self.store.save_review_history(review)
            else:
                self.store.save_review(review)
            audit.append(
                "review.recorded",
                {
                    "decision": review.decision.value,
                    "reviewer": review.reviewer,
                    "reviewed_diff_sha256": review.reviewed_diff_sha256,
                    "commit_subject": review.commit_subject,
                },
                source="reviewer",
            )
            state.review_status = review.decision
            state.delivery_status = (
                DeliveryStatus.COMMIT_PENDING
                if review.decision is ReviewStatus.APPROVED
                else DeliveryStatus.NOT_READY
            )
            self.store.save_state(state)

            permissions_path = run_dir / "permissions.json"
            permissions = (
                self.store.load_permissions(task_id)
                if permissions_path.is_file()
                else {"effective": {"verified": False}}
            )
            result, report = self.report_builder.build(
                task,
                state,
                permissions=permissions,
                changes=changes,
                review=review,
                denied_event_count=audit.denied_event_count(),
            )
            self.store.save_result(result)
            self.store.save_report(task_id, report)
            return review
        finally:
            self.store.release_active_lock(lock)

    @staticmethod
    def _load_changes(run_dir: Path) -> dict[str, object]:
        path = run_dir / "changes/files.json"
        if not path.is_file():
            raise ReviewError("the final diff has not been captured")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InfrastructureError("changes/files.json is unreadable") from exc
        if not isinstance(data, dict):
            raise InfrastructureError("changes/files.json must contain an object")
        return data


__all__ = ["ReviewError", "ReviewService"]
