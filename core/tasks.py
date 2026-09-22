"""Durable task, trace, event, and worker persistence."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from memory.persistent import contains_secret


def _now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
}


@dataclass
class TaskStep:
    id: str
    index: int
    kind: str
    status: str
    summary: str
    tool: str | None = None
    error_code: str | None = None
    duration_ms: float | None = None
    created_at: datetime = field(default_factory=_now)


@dataclass
class TaskEvent:
    id: int
    task_id: str
    type: str
    payload: dict[str, Any]
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScheduledJob:
    id: str
    name: str
    request: str
    run_at: datetime
    interval_seconds: int | None
    next_run_at: datetime
    last_run_at: datetime | None
    enabled: bool
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentTask:
    id: str
    request_id: str
    session_id: str
    original_request: str
    status: TaskStatus = TaskStatus.PENDING
    summary: str | None = None
    current_step: int = 0
    errors: list[str] = field(default_factory=list)
    steps: list[TaskStep] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    worker_id: str | None = None
    attempt_count: int = 0
    priority: int = 0
    cancelled: bool = False

    def add_step(
        self, *, kind: str, status: str, summary: str, **kwargs: Any
    ) -> TaskStep:
        self.current_step += 1
        step = TaskStep(uuid4().hex, self.current_step, kind, status, summary, **kwargs)
        self.steps.append(step)
        self.updated_at = _now()
        return step

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStore:
    """Thread-safe task repository, optionally backed by SQLite."""

    SCHEMA_VERSION = 5

    def __init__(
        self,
        *,
        limit: int = 500,
        path: str | Path | None = None,
        recover_on_start: bool = True,
    ) -> None:
        self.limit = limit
        self.path = Path(path).expanduser() if path is not None else None
        self._tasks: dict[str, AgentTask] = {}
        self._events: list[TaskEvent] = []
        self._event_sequence = 0
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.path, timeout=5.0, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            if recover_on_start:
                self.recover_interrupted()

    def _migrate(self) -> None:
        assert self._connection is not None
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise RuntimeError("Task database was created by a newer Cato version.")
        if version == 0:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE tasks (
                      id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                      session_id TEXT NOT NULL, original_request TEXT NOT NULL,
                      status TEXT NOT NULL, summary TEXT, current_step INTEGER NOT NULL,
                      errors TEXT NOT NULL, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                      worker_id TEXT, attempt_count INTEGER NOT NULL,
                      priority INTEGER NOT NULL, cancelled INTEGER NOT NULL);
                    CREATE INDEX tasks_status_priority_idx
                      ON tasks(status, priority DESC, created_at);
                    CREATE INDEX tasks_session_idx
                      ON tasks(session_id, created_at DESC);
                    CREATE TABLE task_steps (
                      id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                      step_number INTEGER NOT NULL, kind TEXT NOT NULL,
                      status TEXT NOT NULL, summary TEXT NOT NULL, tool_name TEXT,
                      error_code TEXT, duration_ms REAL, created_at TEXT NOT NULL,
                      UNIQUE(task_id, step_number));
                    CREATE TABLE task_events (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      task_id TEXT NOT NULL REFERENCES tasks(id), type TEXT NOT NULL,
                      payload TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE INDEX task_events_task_idx ON task_events(task_id, id);
                    CREATE TABLE workers (
                      id TEXT PRIMARY KEY, status TEXT NOT NULL,
                      started_at TEXT NOT NULL, last_seen TEXT NOT NULL);
                    CREATE TABLE scheduled_jobs (
                      id TEXT PRIMARY KEY, name TEXT NOT NULL, request TEXT NOT NULL,
                      run_at TEXT NOT NULL, interval_seconds INTEGER,
                      next_run_at TEXT NOT NULL, last_run_at TEXT,
                      enabled INTEGER NOT NULL, created_at TEXT NOT NULL);
                    PRAGMA user_version=1;
                    """
                )
            version = 1
        if version < 2:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE task_attempts (
                      id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                      attempt_no INTEGER NOT NULL, worker_id TEXT NOT NULL,
                      pid INTEGER, spawn_token TEXT NOT NULL, started_at TEXT NOT NULL,
                      finished_at TEXT, exit_reason TEXT, exit_code INTEGER,
                      error_code TEXT, UNIQUE(task_id, attempt_no));
                    CREATE INDEX task_attempts_task_idx
                      ON task_attempts(task_id, attempt_no);
                    PRAGMA user_version=2;
                    """
                )
            version = 2
        if version < 3:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE task_idempotency (
                      key_hash TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                      task_id TEXT NOT NULL REFERENCES tasks(id),
                      created_at TEXT NOT NULL);
                    PRAGMA user_version=3;
                    """
                )
            version = 3
        if version < 4:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE task_archives (
                      task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                      archived_at TEXT NOT NULL, reason TEXT NOT NULL);
                    PRAGMA user_version=4;
                    """
                )
            version = 4
        if version < 5:
            with self._connection:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS task_dependencies (
                      task_id TEXT NOT NULL REFERENCES tasks(id),
                      depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
                      PRIMARY KEY(task_id,depends_on_task_id));
                    CREATE TABLE IF NOT EXISTS workspace_locks (
                      workspace TEXT PRIMARY KEY,
                      task_id TEXT NOT NULL REFERENCES tasks(id),
                      mode TEXT NOT NULL, acquired_at TEXT NOT NULL,
                      heartbeat_at TEXT NOT NULL);
                    PRAGMA user_version=5;
                    """
                )

    def create(
        self,
        session_id: str,
        request: str,
        *,
        status: TaskStatus = TaskStatus.PENDING,
        priority: int = 0,
    ) -> AgentTask:
        safe_request = (
            "[sensitive request redacted]" if contains_secret(request) else request
        )
        task = AgentTask(
            uuid4().hex,
            uuid4().hex,
            session_id,
            safe_request,
            status=status,
            priority=max(-1, min(1, priority)),
        )
        with self._lock:
            if self._connection is None:
                self._tasks[task.id] = task
                while len(self._tasks) > self.limit:
                    self._tasks.pop(next(iter(self._tasks)))
            else:
                self._save_sql(task)
            self.append_event(task.id, "task_created", {"status": status.value})
        return task

    def save(self, task: AgentTask) -> None:
        task.updated_at = _now()
        with self._lock:
            if self._connection is None:
                self._tasks[task.id] = task
            else:
                self._save_sql(task)

    def _save_sql(self, task: AgentTask) -> None:
        assert self._connection is not None
        values = (
            task.id,
            task.request_id,
            task.session_id,
            task.original_request,
            task.status.value,
            task.summary,
            task.current_step,
            json.dumps(task.errors),
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
            task.started_at.isoformat() if task.started_at else None,
            task.completed_at.isoformat() if task.completed_at else None,
            task.worker_id,
            task.attempt_count,
            task.priority,
            int(task.cancelled),
        )
        with self._connection:
            self._connection.execute(
                """INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                summary=excluded.summary,current_step=excluded.current_step,
                errors=excluded.errors,updated_at=excluded.updated_at,
                started_at=excluded.started_at,completed_at=excluded.completed_at,
                worker_id=excluded.worker_id,attempt_count=excluded.attempt_count,
                priority=excluded.priority,cancelled=excluded.cancelled""",
                values,
            )
            for step in task.steps:
                self._connection.execute(
                    "INSERT OR REPLACE INTO task_steps VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        step.id,
                        task.id,
                        step.index,
                        step.kind,
                        step.status,
                        step.summary,
                        step.tool,
                        step.error_code,
                        step.duration_ms,
                        step.created_at.isoformat(),
                    ),
                )

    def get(self, task_id: str) -> AgentTask | None:
        with self._lock:
            if self._connection is None:
                return self._tasks.get(task_id)
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return self._from_row(row) if row else None

    def list(
        self,
        *,
        session_id: str | None = None,
        status: TaskStatus | str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[AgentTask]:
        limit = max(1, min(limit, 500))
        with self._lock:
            if self._connection is None:
                tasks = list(self._tasks.values())
                if session_id is not None:
                    tasks = [task for task in tasks if task.session_id == session_id]
                if status is not None:
                    tasks = [task for task in tasks if task.status == status]
                return sorted(tasks, key=lambda task: task.created_at, reverse=True)[
                    :limit
                ]
            clauses, values = [], []
            if session_id is not None:
                clauses.append("session_id=?")
                values.append(session_id)
            if status is not None:
                clauses.append("status=?")
                values.append(TaskStatus(status).value)
            if not include_archived:
                clauses.append(
                    "NOT EXISTS(SELECT 1 FROM task_archives a WHERE a.task_id=tasks.id)"
                )
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = self._connection.execute(
                f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
            return [self._from_row(row) for row in rows]

    def archive(self, task_id: str, reason: str = "manual") -> bool:
        task = self.get(task_id)
        if task is None or task.status not in TERMINAL_STATUSES:
            return False
        if self._connection is None:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO task_archives VALUES(?,?,?)",
                (task_id, _now().isoformat(), reason[:120]),
            )
        if cursor.rowcount:
            self.append_event(task_id, "task_archived", {"reason": reason[:120]})
        return cursor.rowcount == 1

    def archive_expired(self, retention_days: int) -> int:
        if self._connection is None:
            return 0
        cutoff = (_now() - timedelta(days=max(1, retention_days))).isoformat()
        with self._lock, self._connection:
            rows = self._connection.execute(
                """SELECT id FROM tasks WHERE status IN (?,?) AND updated_at<?
                AND NOT EXISTS(
                  SELECT 1 FROM task_archives a WHERE a.task_id=tasks.id
                )""",
                (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, cutoff),
            ).fetchall()
            for row in rows:
                self._connection.execute(
                    "INSERT INTO task_archives VALUES(?,?,?)",
                    (row["id"], _now().isoformat(), "retention"),
                )
        return len(rows)

    def claim_next(self, worker_id: str) -> AgentTask | None:
        with self._lock:
            if self._connection is None:
                candidates = [
                    t for t in self._tasks.values() if t.status == TaskStatus.QUEUED
                ]
                candidates.sort(key=lambda t: (-t.priority, t.created_at))
                task = candidates[0] if candidates else None
                if task:
                    task.status, task.worker_id = TaskStatus.RUNNING, worker_id
                    task.started_at = task.started_at or _now()
                    task.attempt_count += 1
                return task
            assert self._connection is not None
            with self._connection:
                self._block_failed_dependencies()
                row = self._connection.execute(
                    """SELECT id FROM tasks WHERE status=? AND NOT EXISTS (
                      SELECT 1 FROM task_dependencies d JOIN tasks prerequisite
                      ON prerequisite.id=d.depends_on_task_id
                      WHERE d.task_id=tasks.id AND prerequisite.status!='completed')
                    ORDER BY priority DESC,created_at LIMIT 1""",
                    (TaskStatus.QUEUED.value,),
                ).fetchone()
                if row is None:
                    return None
                now = _now().isoformat()
                changed = self._connection.execute(
                    """UPDATE tasks SET status=?,worker_id=?,
                    started_at=COALESCE(started_at,?),updated_at=?,
                    attempt_count=attempt_count+1 WHERE id=? AND status=?""",
                    (
                        TaskStatus.RUNNING.value,
                        worker_id,
                        now,
                        now,
                        row["id"],
                        TaskStatus.QUEUED.value,
                    ),
                )
                if changed.rowcount != 1:
                    return None
            task = self.get(row["id"])
            if task:
                self.append_event(task.id, "task_started", {"worker_id": worker_id})
            return task

    def add_dependency(self, task_id: str, depends_on: str) -> bool:
        if self._connection is None or task_id == depends_on:
            return False
        if self.get(task_id) is None or self.get(depends_on) is None:
            return False
        with self._lock:
            cycle = self._connection.execute(
                """WITH RECURSIVE ancestors(id) AS (
                  SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?
                  UNION SELECT d.depends_on_task_id FROM task_dependencies d
                  JOIN ancestors a ON d.task_id=a.id)
                SELECT 1 FROM ancestors WHERE id=? LIMIT 1""",
                (depends_on, task_id),
            ).fetchone()
            if cycle:
                return False
            with self._connection:
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO task_dependencies VALUES(?,?)",
                    (task_id, depends_on),
                )
        return cursor.rowcount == 1

    def _block_failed_dependencies(self) -> None:
        assert self._connection is not None
        self._connection.execute(
            """UPDATE tasks SET status='blocked',errors='[\"dependency_failed\"]',
            updated_at=? WHERE status='queued' AND EXISTS (
              SELECT 1 FROM task_dependencies d JOIN tasks prerequisite
              ON prerequisite.id=d.depends_on_task_id WHERE d.task_id=tasks.id
              AND prerequisite.status IN ('failed','blocked','cancelled'))""",
            (_now().isoformat(),),
        )

    def acquire_workspace_lock(self, workspace: str, task_id: str, mode: str) -> bool:
        if self._connection is None or mode not in {"read", "write"}:
            return False
        canonical = str(Path(workspace).resolve())
        now = _now().isoformat()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT * FROM workspace_locks WHERE workspace=?", (canonical,)
            ).fetchone()
            if existing and existing["task_id"] != task_id:
                return False
            self._connection.execute(
                "INSERT OR REPLACE INTO workspace_locks VALUES(?,?,?,?,?)",
                (canonical, task_id, mode, now, now),
            )
        return True

    def release_workspace_lock(self, workspace: str, task_id: str) -> bool:
        if self._connection is None:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM workspace_locks WHERE workspace=? AND task_id=?",
                (str(Path(workspace).resolve()), task_id),
            )
        return cursor.rowcount == 1

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status in TERMINAL_STATUSES:
            return False
        task.cancelled = True
        task.status = (
            TaskStatus.CANCELLED
            if task.status in {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.PAUSED}
            else TaskStatus.CANCELLING
        )
        if task.status == TaskStatus.CANCELLED:
            task.completed_at = _now()
        self.save(task)
        self.append_event(task.id, "task_cancel_requested", {})
        return True

    def retry(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status not in {
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.INTERRUPTED,
            TaskStatus.CANCELLED,
        }:
            return False
        task.status = TaskStatus.QUEUED
        task.cancelled = False
        task.worker_id = None
        task.completed_at = None
        task.summary = None
        self.save(task)
        self.append_event(task.id, "task_retried", {"attempt": task.attempt_count + 1})
        return True

    def append_event(
        self, task_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> TaskEvent:
        safe_payload, created = payload or {}, _now()
        with self._lock:
            if self._connection is None:
                self._event_sequence += 1
                event = TaskEvent(
                    self._event_sequence, task_id, event_type, safe_payload, created
                )
                self._events.append(event)
                return event
            assert self._connection is not None
            with self._connection:
                cursor = self._connection.execute(
                    """INSERT INTO task_events(task_id,type,payload,created_at)
                    VALUES(?,?,?,?)""",
                    (
                        task_id,
                        event_type,
                        json.dumps(safe_payload),
                        created.isoformat(),
                    ),
                )
            return TaskEvent(
                cursor.lastrowid or 0, task_id, event_type, safe_payload, created
            )

    def events(
        self, task_id: str, *, after: int = 0, limit: int = 200
    ) -> list[TaskEvent]:
        limit = max(1, min(limit, 1000))
        with self._lock:
            if self._connection is None:
                return [
                    e for e in self._events if e.task_id == task_id and e.id > after
                ][:limit]
            assert self._connection is not None
            rows = self._connection.execute(
                """SELECT * FROM task_events WHERE task_id=? AND id>?
                ORDER BY id LIMIT ?""",
                (task_id, after, limit),
            ).fetchall()
            return [
                TaskEvent(
                    row["id"],
                    row["task_id"],
                    row["type"],
                    json.loads(row["payload"]),
                    datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]

    def recover_interrupted(self) -> int:
        if self._connection is None:
            return 0
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT id,worker_id FROM tasks WHERE status IN (?,?)",
                (
                    TaskStatus.RUNNING.value,
                    TaskStatus.CANCELLING.value,
                ),
            ).fetchall()
            interrupted = [
                row for row in rows if not self._worker_process_alive(row["worker_id"])
            ]
            for row in interrupted:
                self._connection.execute(
                    """UPDATE tasks SET status=?,updated_at=?,worker_id=NULL
                    WHERE id=?""",
                    (TaskStatus.INTERRUPTED.value, _now().isoformat(), row["id"]),
                )
        for row in interrupted:
            self.append_event(row["id"], "task_interrupted", {})
        return len(interrupted)

    @staticmethod
    def _worker_process_alive(worker_id: str | None) -> bool:
        if not worker_id or not worker_id.startswith("local-"):
            return False
        try:
            process_id = int(worker_id.split("-", 2)[1])
            os.kill(process_id, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            return False
        return True

    def worker_heartbeat(self, worker_id: str, status: str = "idle") -> None:
        if self._connection is None:
            return
        now = _now().isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO workers VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,last_seen=excluded.last_seen""",
                (worker_id, status, now, now),
            )

    def workers(self) -> list[dict[str, Any]]:
        if self._connection is None:
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM workers ORDER BY last_seen DESC LIMIT 20"
            ).fetchall()
        return [dict(row) for row in rows]

    def start_attempt(
        self, task: AgentTask, worker_id: str, pid: int, spawn_token: str
    ) -> str:
        attempt_id = uuid4().hex
        if self._connection is None:
            return attempt_id
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO task_attempts
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    task.id,
                    task.attempt_count,
                    worker_id,
                    pid,
                    spawn_token,
                    _now().isoformat(),
                    None,
                    None,
                    None,
                    None,
                ),
            )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        reason: str,
        *,
        exit_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        if self._connection is None:
            return
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE task_attempts SET finished_at=?,exit_reason=?,
                exit_code=?,error_code=? WHERE id=?""",
                (_now().isoformat(), reason, exit_code, error_code, attempt_id),
            )

    def attempts(self, task_id: str) -> list[dict[str, Any]]:
        if self._connection is None:
            return []
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def idempotency(self, key_hash: str) -> tuple[str, str] | None:
        if self._connection is None:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_hash,task_id FROM task_idempotency WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
        return (row["payload_hash"], row["task_id"]) if row else None

    def register_idempotency(
        self, key_hash: str, payload_hash: str, task_id: str
    ) -> bool:
        if self._connection is None:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO task_idempotency
                VALUES(?,?,?,?)""",
                (key_hash, payload_hash, task_id, _now().isoformat()),
            )
        return cursor.rowcount == 1

    def create_schedule(
        self,
        name: str,
        request: str,
        run_at: datetime,
        *,
        interval_seconds: int | None = None,
    ) -> ScheduledJob:
        if run_at.tzinfo is None:
            raise ValueError("Scheduled time must include a timezone.")
        if contains_secret(request):
            raise ValueError("Potential secrets cannot be stored in schedules.")
        if interval_seconds is not None and interval_seconds < 60:
            raise ValueError("Recurring schedules must be at least 60 seconds apart.")
        job = ScheduledJob(
            uuid4().hex,
            name.strip()[:120] or "Scheduled task",
            request,
            run_at.astimezone(UTC),
            interval_seconds,
            run_at.astimezone(UTC),
            None,
            True,
            _now(),
        )
        if self._connection is None:
            raise RuntimeError("Schedules require durable task storage.")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job.id,
                    job.name,
                    job.request,
                    job.run_at.isoformat(),
                    job.interval_seconds,
                    job.next_run_at.isoformat(),
                    None,
                    1,
                    job.created_at.isoformat(),
                ),
            )
        return job

    def schedules(self, *, enabled: bool | None = None) -> list[ScheduledJob]:
        if self._connection is None:
            return []
        where, values = "", ()
        if enabled is not None:
            where, values = " WHERE enabled=?", (int(enabled),)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM scheduled_jobs{where} ORDER BY next_run_at", values
            ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def claim_due_schedules(self, now: datetime | None = None) -> list[ScheduledJob]:
        if self._connection is None:
            return []
        current = (now or _now()).astimezone(UTC)
        with self._lock, self._connection:
            rows = self._connection.execute(
                """SELECT * FROM scheduled_jobs
                WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at""",
                (current.isoformat(),),
            ).fetchall()
            jobs = [self._schedule_from_row(row) for row in rows]
            for job in jobs:
                if job.interval_seconds is None:
                    enabled, next_run = 0, job.next_run_at.isoformat()
                else:
                    enabled = 1
                    next_run = datetime.fromtimestamp(
                        current.timestamp() + job.interval_seconds, UTC
                    ).isoformat()
                self._connection.execute(
                    """UPDATE scheduled_jobs SET last_run_at=?,next_run_at=?,enabled=?
                    WHERE id=? AND next_run_at=?""",
                    (
                        current.isoformat(),
                        next_run,
                        enabled,
                        job.id,
                        job.next_run_at.isoformat(),
                    ),
                )
        return jobs

    @staticmethod
    def _schedule_from_row(row: sqlite3.Row) -> ScheduledJob:
        return ScheduledJob(
            row["id"],
            row["name"],
            row["request"],
            datetime.fromisoformat(row["run_at"]),
            row["interval_seconds"],
            datetime.fromisoformat(row["next_run_at"]),
            datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None,
            bool(row["enabled"]),
            datetime.fromisoformat(row["created_at"]),
        )

    def _from_row(self, row: sqlite3.Row) -> AgentTask:
        assert self._connection is not None
        step_rows = self._connection.execute(
            "SELECT * FROM task_steps WHERE task_id=? ORDER BY step_number",
            (row["id"],),
        ).fetchall()
        steps = [
            TaskStep(
                item["id"],
                item["step_number"],
                item["kind"],
                item["status"],
                item["summary"],
                item["tool_name"],
                item["error_code"],
                item["duration_ms"],
                datetime.fromisoformat(item["created_at"]),
            )
            for item in step_rows
        ]
        return AgentTask(
            row["id"],
            row["request_id"],
            row["session_id"],
            row["original_request"],
            TaskStatus(row["status"]),
            row["summary"],
            row["current_step"],
            json.loads(row["errors"]),
            steps,
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]),
            datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            datetime.fromisoformat(row["completed_at"])
            if row["completed_at"]
            else None,
            row["worker_id"],
            row["attempt_count"],
            row["priority"],
            bool(row["cancelled"]),
        )

    def close(self) -> None:
        if self._connection is not None:
            with self._lock:
                self._connection.close()
