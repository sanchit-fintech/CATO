from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api import create_app
from core.approvals import ApprovalStore
from core.cato import Cato
from core.config import ConfigurationError, Settings
from core.llm.fake import FakeModelProvider
from core.task_service import TaskService
from core.tasks import TaskStatus


class BlockingProvider:
    def next_action(self, command, context, tools):
        time.sleep(30)
        return {"type": "final", "response": "too late"}


class DescendantProvider:
    def __init__(self, pid_file: Path) -> None:
        self.pid_file = pid_file

    def next_action(self, command, context, tools):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.pid_file.write_text(str(child.pid))
        child.wait()
        return {"type": "final", "response": "too late"}


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "task_database_path": tmp_path / "tasks.sqlite3",
        "approval_database_path": tmp_path / "approvals.sqlite3",
        "memory_path": tmp_path / "memory.sqlite3",
    }
    values.update(overrides)
    return Settings(None, "fake", (tmp_path,), "INFO", **values)


def test_process_worker_hard_cancel_reaps_child(tmp_path: Path) -> None:
    cato = Cato(
        provider=BlockingProvider(),
        settings=settings(tmp_path),
        platform_system="Linux",
    )
    service = TaskService(cato, poll_interval=0.01)
    task = service.submit("block")
    for _ in range(200):
        if service.active_pid:
            break
        time.sleep(0.01)
    pid = service.active_pid
    assert pid is not None
    assert cato.tasks.cancel(task.id)
    for _ in range(300):
        current = cato.tasks.get(task.id)
        if current.status == "cancelled":
            break
        time.sleep(0.01)
    assert current.status == "cancelled"
    service.stop()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_hard_cancel_terminates_descendant_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    cato = Cato(
        provider=DescendantProvider(pid_file),
        settings=settings(tmp_path),
        platform_system="Linux",
    )
    service = TaskService(cato, poll_interval=0.01)
    task = service.submit("spawn descendant")
    for _ in range(300):
        if pid_file.exists():
            break
        time.sleep(0.01)
    descendant_pid = int(pid_file.read_text())
    assert cato.tasks.cancel(task.id)
    for _ in range(300):
        if cato.tasks.get(task.id).status == "cancelled":
            break
        time.sleep(0.01)
    service.stop()
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


def test_durable_approval_survives_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "approvals.sqlite3"
    first = ApprovalStore(ttl_seconds=60, path=path)
    approval = first.create(
        task_id="task",
        session_id="session",
        tool="file_move",
        arguments={"source": "a", "destination": "b"},
        request="move a",
        summary="Move a to b",
        risk="moderate",
    )
    second = ApprovalStore(ttl_seconds=60, path=path)
    decision = second.approve(approval.id, "session")
    assert decision.ok
    assert decision.approval.arguments["source"] == "a"
    assert second.approve(approval.id, "session").code == "approval_used"


def test_api_auth_and_idempotency(tmp_path: Path) -> None:
    cato = Cato(
        provider=FakeModelProvider(responses=["done"]),
        settings=settings(tmp_path, api_token="test-token"),
        platform_system="Linux",
    )
    headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "same"}
    with TestClient(create_app(cato)) as client:
        assert client.get("/health").status_code == 401
        first = client.post("/tasks", json={"request": "inspect"}, headers=headers)
        second = client.post("/tasks", json={"request": "inspect"}, headers=headers)
        conflict = client.post("/tasks", json={"request": "different"}, headers=headers)
    assert first.status_code == 202
    assert second.json()["task_id"] == first.json()["task_id"]
    assert conflict.status_code == 409


def test_sse_reconnect_starts_after_last_event(tmp_path: Path) -> None:
    cato = Cato(
        provider=FakeModelProvider(),
        settings=settings(tmp_path),
        platform_system="Linux",
    )
    task = cato.tasks.create("session", "done")
    first = cato.tasks.append_event(task.id, "first", {})
    cato.tasks.append_event(task.id, "second", {})
    task.status = TaskStatus.COMPLETED
    cato.tasks.save(task)
    with TestClient(create_app(cato)) as client:
        response = client.get(
            f"/tasks/{task.id}/events/stream",
            headers={"Last-Event-ID": str(first.id)},
        )
    assert "event: second" in response.text
    assert "event: first" not in response.text


def test_non_loopback_host_requires_auth(monkeypatch) -> None:
    monkeypatch.setenv("CATO_API_HOST", "0.0.0.0")
    monkeypatch.setenv("CATO_API_TOKEN", "")
    with pytest.raises(ConfigurationError):
        Settings.load(require_api_key=False)


def test_archived_task_is_hidden_but_still_inspectable(tmp_path: Path) -> None:
    cato = Cato(
        provider=FakeModelProvider(responses=["done"]),
        settings=settings(tmp_path),
        platform_system="Linux",
    )
    result = cato.run("finish")
    assert cato.tasks.archive(result.task_id)
    assert cato.tasks.list() == []
    assert cato.tasks.get(result.task_id).status == "completed"


def test_task_database_migrates_from_v2_without_losing_tasks(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    from core.tasks import TaskStore

    initial = TaskStore(path=path)
    task = initial.create("session", "preserve me")
    initial.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE task_archives")
    connection.execute("DROP TABLE task_idempotency")
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()
    upgraded = TaskStore(path=path)
    assert upgraded.get(task.id).original_request == "preserve me"
    assert upgraded.archive(task.id) is False
