"""In-process asynchronous task manager with de-duplication and cancellation."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import AndroidMcpError, now_iso, ok


@dataclass
class TaskRecord:
    task_id: str
    task_type: str
    dedupe_key: str | None
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    status: str = "PENDING"
    progress: float = 0.0
    current_step: str | None = None
    total_steps: int | None = None
    message: str = "排队中"
    result: Any = None
    error: dict[str, Any] | None = None
    cancel_requested: bool = False
    future: Future[Any] | None = None

    def snapshot(self, include_result: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "dedupe_key": self.dedupe_key,
            "status": self.status.lower(),
            "progress": self.progress,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cancel_requested": self.cancel_requested,
        }
        if include_result:
            data["result"] = self.result
            data["error"] = self.error
        return data


class TaskManager:
    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="android-mcp")
        self._tasks: dict[str, TaskRecord] = {}
        self._dedupe: dict[str, str] = {}
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def submit(self, task_type: str, fn: Callable[[Callable[..., None], Callable[[], bool]], Any], *, dedupe_key: str | None = None) -> TaskRecord:
        with self._lock:
            if dedupe_key:
                existing_id = self._dedupe.get(dedupe_key)
                existing = self._tasks.get(existing_id or "")
                if existing and existing.status in {"PENDING", "RUNNING"}:
                    return existing
            task = TaskRecord(task_id=f"task_{uuid.uuid4().hex[:12]}", task_type=task_type, dedupe_key=dedupe_key)
            self._tasks[task.task_id] = task
            if dedupe_key:
                self._dedupe[dedupe_key] = task.task_id
            task.future = self._executor.submit(self._run, task, fn)
            self._changed.notify_all()
            return task

    def _run(self, task: TaskRecord, fn: Callable[[Callable[..., None], Callable[[], bool]], Any]) -> None:
        with self._lock:
            task.status = "RUNNING"
            task.started_at = now_iso()
            task.message = "开始执行"
            self._changed.notify_all()

        def progress(*, current_step: str | None = None, progress: float | None = None, total_steps: int | None = None, message: str | None = None) -> None:
            with self._lock:
                if current_step is not None:
                    task.current_step = current_step
                if progress is not None:
                    task.progress = max(0.0, min(100.0, float(progress)))
                if total_steps is not None:
                    task.total_steps = total_steps
                if message is not None:
                    task.message = message
                self._changed.notify_all()

        def cancelled() -> bool:
            with self._lock:
                return task.cancel_requested

        try:
            result = fn(progress, cancelled)
            with self._lock:
                task.result = result
                task.status = "CANCELLED" if task.cancel_requested else "COMPLETED"
                task.progress = 100.0 if task.status == "COMPLETED" else task.progress
                task.message = "已取消" if task.status == "CANCELLED" else "执行完成"
                task.completed_at = now_iso()
                self._changed.notify_all()
        except AndroidMcpError as exc:
            with self._lock:
                task.status = "CANCELLED" if task.cancel_requested else "FAILED"
                task.error = {"code": exc.code, "message": exc.message, "hint": exc.hint}
                task.message = exc.message
                task.completed_at = now_iso()
                self._changed.notify_all()
        except Exception as exc:  # pragma: no cover - defensive boundary for worker code
            with self._lock:
                task.status = "CANCELLED" if task.cancel_requested else "FAILED"
                task.error = {"code": "task_failed", "message": str(exc)}
                task.message = str(exc)
                task.completed_at = now_iso()
                self._changed.notify_all()

    def get(self, task_id: str, *, include_result: bool = False) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise AndroidMcpError(f"任务不存在：{task_id}", code="task_not_found")
            return ok(task.snapshot(include_result=include_result))

    def list(self, *, task_type: str | None = None, limit: int = 50) -> dict[str, Any]:
        with self._lock:
            records = list(self._tasks.values())
            if task_type:
                records = [item for item in records if item.task_type == task_type]
            records = sorted(records, key=lambda item: item.created_at, reverse=True)[: max(1, min(limit, 200))]
            return ok({"tasks": [item.snapshot() for item in records], "count": len(records)})

    def wait(self, task_id: str, seconds: float) -> dict[str, Any]:
        seconds = max(0.0, min(float(seconds), 60.0))
        deadline = time.monotonic() + seconds
        with self._changed:
            task = self._tasks.get(task_id)
            if not task:
                raise AndroidMcpError(f"任务不存在：{task_id}", code="task_not_found")
            initial = (task.status, task.progress, task.message)
            while task.status in {"PENDING", "RUNNING"} and time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                self._changed.wait(timeout=remaining)
                if (task.status, task.progress, task.message) != initial:
                    break
            return ok(task.snapshot(include_result=task.status in {"COMPLETED", "FAILED", "CANCELLED"}))

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise AndroidMcpError(f"任务不存在：{task_id}", code="task_not_found")
            if task.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return ok(task.snapshot(), hint="任务已经处于终态。")
            task.cancel_requested = True
            task.message = "正在取消；当前步骤结束后退出"
            self._changed.notify_all()
            return ok(task.snapshot(), hint="取消会在构建/设备操作边界生效。")
