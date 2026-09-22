"""Process-isolated background task supervision."""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import time
from multiprocessing.process import BaseProcess
from threading import Event, Thread
from uuid import uuid4

from core.tasks import TERMINAL_STATUSES, AgentTask, TaskStatus
from memory.persistent import contains_secret

logger = logging.getLogger(__name__)


def _sanitize_child_environment() -> None:
    sensitive_suffixes = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")
    for name in list(os.environ):
        if name.upper().endswith(sensitive_suffixes):
            os.environ.pop(name, None)


def _run_task_child(provider: object | None, settings: object, task_id: str) -> None:
    """Reopen state inside an isolated process and execute exactly one task."""
    if hasattr(os, "setsid"):
        os.setsid()
    _sanitize_child_environment()
    from core.cato import Cato

    cato = Cato(
        provider=provider,
        settings=settings,
        platform_system="Linux",
        recover_tasks=False,
    )
    task = cato.tasks.get(task_id)
    if task is None:
        raise RuntimeError("Claimed task disappeared before process startup.")
    cato.execute_task(task)


class TaskService:
    """Supervises one owned child process at a time over the durable queue."""

    def __init__(self, cato: object, *, poll_interval: float = 0.1) -> None:
        self.cato = cato
        self.tasks = cato.tasks
        self.poll_interval = poll_interval
        self.worker_id = f"supervisor-{os.getpid()}-{uuid4().hex[:12]}"
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._process: BaseProcess | None = None
        self._process_task_id: str | None = None
        self._context = multiprocessing.get_context("spawn")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def active_pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        if self.running:
            return
        self.tasks.archive_expired(self.cato.settings.task_retention_days)
        self._stop.clear()
        self._thread = Thread(target=self._work, name="cato-supervisor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._process is not None and self._process.is_alive():
            self._terminate_owned_process("server_shutdown")
        if self._thread is not None:
            self._thread.join(timeout)

    def submit(
        self, request: str, *, session_id: str | None = None, priority: int = 0
    ) -> AgentTask:
        if contains_secret(request):
            raise ValueError("Potential secrets cannot be submitted as durable tasks.")
        session = self.cato.memory.get(session_id) if session_id else None
        session = session or self.cato.memory.create()
        task = self.tasks.create(
            session.id, request, status=TaskStatus.QUEUED, priority=priority
        )
        self.tasks.append_event(task.id, "task_queued", {"priority": priority})
        self.start()
        self._wake.set()
        return task

    def _work(self) -> None:
        self.tasks.worker_heartbeat(self.worker_id, "idle")
        while not self._stop.is_set():
            for job in self.tasks.claim_due_schedules():
                task = self.submit(job.request)
                self.tasks.append_event(
                    task.id, "scheduled_task_created", {"schedule_id": job.id}
                )
            task = self.tasks.claim_next(self.worker_id)
            if task is None:
                self.tasks.worker_heartbeat(self.worker_id, "idle")
                self._wake.wait(self.poll_interval)
                self._wake.clear()
                continue
            self._execute_process(task)
        self.tasks.worker_heartbeat(self.worker_id, "stopped")

    def _execute_process(self, task: AgentTask) -> None:
        spawn_token = uuid4().hex
        process = self._context.Process(
            target=_run_task_child,
            args=(
                None
                if type(self.cato.provider).__name__ == "GeminiModelProvider"
                else self.cato.provider,
                self.cato.settings,
                task.id,
            ),
            name=f"cato-task-{task.id[:8]}",
        )
        started = time.monotonic()
        try:
            process.start()
        except Exception as error:
            self._mark_failed(task.id, "process_start_failed")
            logger.error(
                "task_process_start_failed",
                extra={"task_id": task.id, "error_type": type(error).__name__},
            )
            return
        self._process, self._process_task_id = process, task.id
        assert process.pid is not None
        attempt_id = self.tasks.start_attempt(
            task, self.worker_id, process.pid, spawn_token
        )
        self.tasks.append_event(
            task.id,
            "worker_spawned",
            {"pid": process.pid, "attempt": task.attempt_count},
        )
        self.tasks.worker_heartbeat(self.worker_id, "running")
        reason = "normal"
        while process.is_alive():
            current = self.tasks.get(task.id)
            if current is not None and current.status == TaskStatus.CANCELLING:
                reason = "cancelled"
                self._terminate_owned_process(reason)
                break
            if (
                time.monotonic() - started
                >= self.cato.settings.max_agent_runtime_seconds
            ):
                reason = "timeout"
                self._terminate_owned_process(reason)
                break
            if self._stop.wait(self.poll_interval):
                reason = "server_shutdown"
                self._terminate_owned_process(reason)
                break
        process.join(timeout=1)
        exit_code = process.exitcode
        current = self.tasks.get(task.id)
        if reason in {"cancelled", "timeout"}:
            assert current is not None
            current.status = (
                TaskStatus.CANCELLED if reason == "cancelled" else TaskStatus.FAILED
            )
            current.cancelled = reason == "cancelled"
            current.summary = (
                "Task cancelled and its process was terminated."
                if reason == "cancelled"
                else "Task exceeded its hard runtime limit."
            )
            current.errors.append(
                "process_timeout" if reason == "timeout" else "cancelled"
            )
            self.tasks.save(current)
            self.tasks.append_event(
                task.id,
                "task_cancelled" if reason == "cancelled" else "process_timeout",
                {},
            )
        elif reason == "server_shutdown" and current is not None:
            self._mark_interrupted(task.id, "server_shutdown")
        elif current is not None and current.status not in TERMINAL_STATUSES | {
            TaskStatus.WAITING_FOR_APPROVAL
        }:
            reason = "process_crash"
            self._mark_failed(task.id, "process_crash")
        self.tasks.finish_attempt(
            attempt_id,
            reason,
            exit_code=exit_code,
            error_code=None if reason == "normal" else reason,
        )
        self.tasks.append_event(
            task.id, "worker_terminated", {"reason": reason, "exit_code": exit_code}
        )
        self._process, self._process_task_id = None, None

    def _terminate_owned_process(self, reason: str) -> None:
        process = self._process
        if process is None or process.pid is None or not process.is_alive():
            return
        # Only the live multiprocessing handle created by this supervisor is used.
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                if hasattr(os, "killpg"):
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.join(timeout=1)
        except PermissionError:
            # The child may not have completed setsid() during immediate shutdown.
            process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        except ProcessLookupError:
            process.join(timeout=1)
        logger.info("task_process_terminated", extra={"reason": reason})

    def _mark_failed(self, task_id: str, error_code: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        task.status = TaskStatus.FAILED
        task.errors.append(error_code)
        task.summary = "The isolated task process failed safely."
        self.tasks.save(task)
        self.tasks.append_event(task.id, "task_failed", {"error_code": error_code})

    def _mark_interrupted(self, task_id: str, reason: str) -> None:
        task = self.tasks.get(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return
        task.status = TaskStatus.INTERRUPTED
        task.worker_id = None
        task.summary = "Task execution was interrupted by a safe server shutdown."
        self.tasks.save(task)
        self.tasks.append_event(task.id, "task_interrupted", {"reason": reason})
