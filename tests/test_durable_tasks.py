from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from core.api import create_app
from core.cato import Cato
from core.config import Settings
from core.llm.fake import FakeModelProvider
from core.task_service import TaskService
from core.tasks import TaskStatus, TaskStore


def test_task_steps_and_events_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    first = TaskStore(path=path)
    task = first.create("session", "inspect", status=TaskStatus.QUEUED)
    task.add_step(kind="tool", status="completed", summary="Inspected project")
    first.save(task)
    first.append_event(task.id, "tool_completed", {"tool": "project_inspect"})
    first.close()

    second = TaskStore(path=path)
    restored = second.get(task.id)
    assert restored is not None
    assert restored.steps[0].summary == "Inspected project"
    assert second.events(task.id)[-1].type == "tool_completed"


def test_running_task_becomes_interrupted_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    first = TaskStore(path=path)
    task = first.create("session", "inspect", status=TaskStatus.QUEUED)
    assert first.claim_next("worker-a").id == task.id
    first.close()

    second = TaskStore(path=path)
    assert second.get(task.id).status == TaskStatus.INTERRUPTED
    assert second.events(task.id)[-1].type == "task_interrupted"


def test_atomic_claim_only_returns_task_once(tmp_path: Path) -> None:
    store = TaskStore(path=tmp_path / "tasks.sqlite3")
    task = store.create("session", "inspect", status=TaskStatus.QUEUED)
    assert store.claim_next("worker-a").id == task.id
    assert store.claim_next("worker-b") is None


def test_two_store_connections_cannot_claim_same_task(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    first = TaskStore(path=path)
    task = first.create("session", "inspect", status=TaskStatus.QUEUED)
    second = TaskStore(path=path)
    assert first.claim_next("worker-a").id == task.id
    assert second.claim_next("worker-b") is None


def test_async_api_submission_completes_and_emits_events(tmp_path: Path) -> None:
    settings = Settings(
        None,
        "fake",
        (tmp_path,),
        "INFO",
        task_database_path=tmp_path / "tasks.sqlite3",
    )
    cato = Cato(
        provider=FakeModelProvider(responses=["background complete"]),
        settings=settings,
        platform_system="Linux",
    )
    with TestClient(create_app(cato)) as client:
        submitted = client.post("/tasks", json={"request": "inspect"})
        assert submitted.status_code == 202
        task_id = submitted.json()["task_id"]
        for _ in range(100):
            task = client.get(f"/tasks/{task_id}").json()
            if task["status"] == "completed":
                break
            time.sleep(0.01)
        assert task["summary"] == "background complete"
        events = client.get(f"/tasks/{task_id}/events").json()
        assert events[0]["type"] == "task_created"
        assert events[-1]["type"] == "task_completed"


def test_queued_task_cancels_without_execution(tmp_path: Path) -> None:
    store = TaskStore(path=tmp_path / "tasks.sqlite3")
    task = store.create("session", "wait", status=TaskStatus.QUEUED)
    assert store.cancel(task.id)
    assert store.get(task.id).status == TaskStatus.CANCELLED
    assert store.claim_next("worker") is None


def test_task_store_redacts_sensitive_request(tmp_path: Path) -> None:
    store = TaskStore(path=tmp_path / "tasks.sqlite3")
    task = store.create("session", "api_key=definitely-sensitive-value")
    assert task.original_request == "[sensitive request redacted]"


def test_schedule_survives_restart_and_is_claimed_once(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    first = TaskStore(path=path)
    job = first.create_schedule(
        "inspect later", "inspect project", datetime.now(UTC) + timedelta(seconds=5)
    )
    first.close()

    second = TaskStore(path=path)
    assert second.schedules()[0].id == job.id
    due = second.claim_due_schedules(datetime.now(UTC) + timedelta(seconds=10))
    assert [item.id for item in due] == [job.id]
    assert second.claim_due_schedules(datetime.now(UTC) + timedelta(seconds=10)) == []


def test_dependency_order_cycle_and_failure_blocking(tmp_path: Path) -> None:
    store = TaskStore(path=tmp_path / "tasks.sqlite3")
    first = store.create("session", "first", status=TaskStatus.QUEUED)
    second = store.create("session", "second", status=TaskStatus.QUEUED)
    assert store.add_dependency(second.id, first.id)
    assert not store.add_dependency(first.id, second.id)
    assert store.claim_next("worker").id == first.id
    first = store.get(first.id)
    first.status = TaskStatus.FAILED
    store.save(first)
    assert store.claim_next("worker") is None
    assert store.get(second.id).status == TaskStatus.BLOCKED


def test_workspace_write_lock_is_exclusive(tmp_path: Path) -> None:
    store = TaskStore(path=tmp_path / "tasks.sqlite3")
    first = store.create("session", "first")
    second = store.create("session", "second")
    assert store.acquire_workspace_lock(str(tmp_path), first.id, "write")
    assert not store.acquire_workspace_lock(str(tmp_path), second.id, "write")
    assert store.release_workspace_lock(str(tmp_path), first.id)
    assert store.acquire_workspace_lock(str(tmp_path), second.id, "write")


def test_task_store_waits_briefly_for_multi_process_database_locks(
    tmp_path: Path,
) -> None:
    store = TaskStore(path=tmp_path / "tasks.sqlite3")
    assert store._connection is not None
    timeout = store._connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000


def test_safe_server_shutdown_leaves_task_retriable(tmp_path: Path) -> None:
    settings = Settings(
        None,
        "fake",
        (tmp_path,),
        "INFO",
        task_database_path=tmp_path / "tasks.sqlite3",
    )
    cato = Cato(
        provider=FakeModelProvider(responses=["unused"]),
        settings=settings,
        platform_system="Linux",
    )
    task = cato.tasks.create("session", "inspect", status=TaskStatus.QUEUED)
    assert cato.tasks.claim_next("supervisor-test") is not None

    TaskService(cato)._mark_interrupted(task.id, "server_shutdown")

    interrupted = cato.tasks.get(task.id)
    assert interrupted is not None
    assert interrupted.status == TaskStatus.INTERRUPTED
    assert interrupted.worker_id is None
    assert cato.tasks.retry(task.id)
    retried = cato.tasks.get(task.id)
    assert retried is not None
    assert retried.status == TaskStatus.QUEUED
