from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import BoundedSemaphore, RLock
from typing import Callable, TypeVar

from codex_loop.models import RunResult, TaskSpec


Prepared = TypeVar("Prepared")


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    identifier: str
    task: TaskSpec | None
    future: Future[object]


class TaskExecutor:
    """Run at most one blocking orchestrator workflow outside the event loop."""

    def __init__(self, *, global_gate: BoundedSemaphore | None = None) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="codex-orchestrator",
        )
        self._records: dict[str, ExecutionRecord] = {}
        self._lock = RLock()
        self._closed = False
        self._global_gate = global_gate

    def _run(self, operation: Callable[..., object], *args: object) -> object:
        if self._global_gate is None:
            return operation(*args)
        with self._global_gate:
            return operation(*args)

    def submit(
        self,
        task: TaskSpec,
        operation: Callable[[], RunResult],
    ) -> ExecutionRecord:
        return self.submit_operation(task.task_id, operation, task=task)

    def submit_operation(
        self,
        identifier: str,
        operation: Callable[[], object],
        *,
        task: TaskSpec | None = None,
    ) -> ExecutionRecord:
        with self._lock:
            if self._closed:
                raise RuntimeError("Task executor is closed")
            if self.active_task_id() is not None:
                raise RuntimeError("Another task is already running")
            future = self._pool.submit(self._run, operation)
            record = ExecutionRecord(
                identifier=identifier,
                task=task,
                future=future,
            )
            self._records[identifier] = record
            return record

    def prepare_and_submit(
        self,
        identifier: str,
        prepare: Callable[[], Prepared],
        operation: Callable[[Prepared], object],
    ) -> tuple[Prepared, ExecutionRecord]:
        """Persist work and reserve the one worker as one critical section."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Task executor is closed")
            if self.active_task_id() is not None:
                raise RuntimeError("Another task is already running")
            prepared = prepare()
            future = self._pool.submit(self._run, operation, prepared)
            record = ExecutionRecord(
                identifier=identifier,
                task=None,
                future=future,
            )
            self._records[identifier] = record
            return prepared, record

    def active_task_id(self) -> str | None:
        with self._lock:
            for task_id, record in self._records.items():
                if not record.future.done():
                    return task_id
        return None

    def get(self, task_id: str) -> ExecutionRecord | None:
        with self._lock:
            return self._records.get(task_id)

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=False)
