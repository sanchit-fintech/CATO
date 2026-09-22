from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api import create_app
from core.cato import Cato
from core.config import Settings
from core.llm.fake import FakeModelProvider
from memory.persistent import MemoryRefused, SQLiteMemory
from tools.project import discover_projects, inspect_project


def test_sqlite_memory_survives_restart_and_ranks_matches(tmp_path: Path) -> None:
    database = tmp_path / "memory.sqlite3"
    first = SQLiteMemory(database)
    stored = first.store("preference", "Use pytest for the Cato project", importance=8)
    first.store("fact", "The garden is green", importance=10)
    first.close()

    second = SQLiteMemory(database)
    assert second.get(stored.id) is not None
    assert [record.id for record in second.search("Cato pytest")] == [stored.id]
    assert second.export()[0]["content"]
    second.close()


def test_memory_fts_ranks_relevant_project_path(tmp_path: Path) -> None:
    memory = SQLiteMemory(tmp_path / "memory.sqlite3")
    relevant = memory.store("project", "FinSight lives at /workspace/finsight")
    memory.store("project", "Garden notes live in Documents")
    results = memory.search("FinSight workspace")
    assert results[0].id == relevant.id


@pytest.mark.parametrize(
    "value",
    ["api_key=very-secret-value", "password: hunter2", "-----BEGIN PRIVATE KEY-----"],
)
def test_sqlite_memory_refuses_likely_secrets(tmp_path: Path, value: str) -> None:
    memory = SQLiteMemory(tmp_path / "memory.sqlite3")
    with pytest.raises(MemoryRefused):
        memory.store("fact", value)


def test_project_tools_discover_and_inspect_within_root(tmp_path: Path) -> None:
    project = tmp_path / "sample"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='sample'\n")
    (project / "tests").mkdir()

    discovered = discover_projects(str(tmp_path), approved_roots=(tmp_path,))
    inspected = inspect_project(str(project), approved_roots=(tmp_path,))

    assert discovered.ok
    assert discovered.data["projects"][0]["path"] == str(project.resolve())
    assert inspected.ok
    assert inspected.data["test_command"] == ["python", "-m", "pytest"]


def test_project_tools_reject_outside_root(tmp_path: Path) -> None:
    result = inspect_project("/", approved_roots=(tmp_path,))
    assert not result.ok
    assert result.code == "path_not_approved"


def test_cato_registers_memory_and_project_tools(tmp_path: Path) -> None:
    settings = Settings(None, "fake", (tmp_path,), "INFO", memory_path=tmp_path / "db")
    cato = Cato(
        provider=FakeModelProvider(), settings=settings, platform_system="Linux"
    )
    names = cato.tools.list_tools()
    assert {
        "memory_store",
        "memory_search",
        "project_discover",
        "project_inspect",
    } <= set(names)


def test_runtime_stops_repeated_equivalent_actions(tmp_path: Path) -> None:
    decisions = [
        {
            "type": "tool",
            "tool": "filesystem_list",
            "arguments": {"path": str(tmp_path)},
        }
    ] * 3
    settings = Settings(
        None,
        "fake",
        (tmp_path,),
        "INFO",
        repeated_action_limit=3,
    )
    cato = Cato(
        provider=FakeModelProvider(decisions),
        settings=settings,
        platform_system="Linux",
    )
    result = cato.run("keep listing")
    assert result.status == "stalled"
    assert result.task_id
    assert cato.tasks.get(result.task_id).errors == ["repeated_action"]


def test_api_exposes_tools_readiness_and_task_trace(tmp_path: Path) -> None:
    settings = Settings(None, "fake", (tmp_path,), "INFO")
    cato = Cato(
        provider=FakeModelProvider(responses=["finished"]),
        settings=settings,
        platform_system="Linux",
    )
    client = TestClient(create_app(cato))

    chat = client.post("/chat", json={"message": "hello"}).json()
    task = client.get(f"/tasks/{chat['task_id']}").json()

    assert client.get("/readiness").json()["status"] == "ready"
    assert any(
        tool["name"] == "project_inspect" for tool in client.get("/tools").json()
    )
    assert task["request_id"] == chat["request_id"]
    assert task["status"] == "completed"
    assert client.get("/tasks").json()[0]["id"] == chat["task_id"]
