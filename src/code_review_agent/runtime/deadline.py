"""并发任务独立的截止时间；子任务继承作用域，不改写共享 StageRunner。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskDeadline:
    at: float | None
    error: str

    @property
    def expired(self) -> bool:
        return self.at is not None and time.monotonic() >= self.at


TASK_DEADLINE: ContextVar[TaskDeadline | None] = ContextVar("task_deadline", default=None)


@contextmanager
def deadline_scope(deadline: TaskDeadline | None) -> Iterator[None]:
    token = TASK_DEADLINE.set(deadline)
    try:
        yield
    finally:
        TASK_DEADLINE.reset(token)


def effective_deadline(overall: float | None) -> TaskDeadline:
    task = TASK_DEADLINE.get()
    if task is not None and task.at is not None and (overall is None or task.at <= overall):
        return task
    return TaskDeadline(overall, "Review timeout: PR/run deadline exhausted")
