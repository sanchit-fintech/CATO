import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from core.api import create_app
from core.cato import Cato
from core.config import Settings
from core.llm.base import ProviderError
from core.llm.fake import FakeModelProvider
from core.tool_types import ToolArgument, ToolDefinition
from memory.store import InMemoryStore
from tools.command import run_command
from tools.file_read import read_file
from tools.filesystem import (
    copy_path,
    create_directory,
    list_files,
    move_path,
    stat_file,
    write_file,
)


def config(root: Path, *, iterations: int = 8, history: int = 50) -> Settings:
    return Settings(
        None, "fake", (root,), "CRITICAL", iterations, 1_000_000, 2_000, 1, history
    )


def test_multi_step_agent_loop(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("hello")
    provider = FakeModelProvider(
        decisions=[
            {
                "type": "tool",
                "tool": "filesystem_stat",
                "arguments": {"path": str(note)},
                "plan": ["inspect", "read"],
            },
            {"type": "tool", "tool": "file_read", "arguments": {"path": str(note)}},
            {"type": "final", "response": "It is a text note saying hello."},
        ]
    )
    result = Cato(provider=provider, settings=config(tmp_path)).run("inspect and read")
    assert result.status == "completed" and result.iterations == 3
    assert "hello" in result.response


def test_tool_failure_can_be_followed_by_recovery(tmp_path: Path) -> None:
    note = tmp_path / "found.txt"
    note.write_text("ok")
    provider = FakeModelProvider(
        decisions=[
            {
                "type": "tool",
                "tool": "file_read",
                "arguments": {"path": str(tmp_path / "missing")},
            },
            {"type": "tool", "tool": "file_read", "arguments": {"path": str(note)}},
            {"type": "final", "response": "Recovered."},
        ]
    )
    assert (
        Cato(provider=provider, settings=config(tmp_path)).respond("read")
        == "Recovered."
    )


def test_max_iterations_stops_loop(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        decisions=[
            {
                "type": "tool",
                "tool": "filesystem_list",
                "arguments": {"path": str(tmp_path)},
            }
        ]
        * 4
    )
    result = Cato(provider=provider, settings=config(tmp_path, iterations=2)).run(
        "loop"
    )
    assert result.status == "max_iterations" and result.iterations == 2


def test_provider_exception_after_observation_is_safe(tmp_path: Path) -> None:
    class Provider:
        calls = 0

        def next_action(self, command, context, tools):
            self.calls += 1
            if self.calls == 1:
                return {
                    "type": "tool",
                    "tool": "filesystem_list",
                    "arguments": {"path": str(tmp_path)},
                }
            raise ProviderError("private")

    result = Cato(provider=Provider(), settings=config(tmp_path)).run("list")
    assert result.status == "failed" and "private" not in result.response


def test_filesystem_operations_and_overwrite_protection(tmp_path: Path) -> None:
    made = create_directory(str(tmp_path / "folder"), approved_roots=(tmp_path,))
    source = tmp_path / "source.txt"
    source.write_text("data")
    destination = tmp_path / "copy.txt"
    assert (
        made.ok
        and stat_file(str(tmp_path / "folder"), approved_roots=(tmp_path,)).data["type"]
        == "directory"
    )
    assert copy_path(str(source), str(destination), approved_roots=(tmp_path,)).ok
    assert not copy_path(str(source), str(destination), approved_roots=(tmp_path,)).ok
    moved = tmp_path / "moved.txt"
    assert move_path(str(destination), str(moved), approved_roots=(tmp_path,)).ok
    assert moved.read_text() == "data" and not destination.exists()
    assert (
        write_file(str(moved), "lost", approved_roots=(tmp_path,)).code
        == "already_exists"
    )


def test_list_is_bounded_and_hides_sensitive(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".env").write_text("secret")
    result = list_files(str(tmp_path), approved_roots=(tmp_path,), max_results=1)
    assert result.ok and result.truncated and len(result.data) == 1
    assert all(item["name"] != ".env" for item in result.data)


def test_directory_copy_rejects_symlink_escape(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    source = approved / "source"
    source.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (source / "escape").symlink_to(outside)
    result = copy_path(str(source), str(approved / "copy"), approved_roots=(approved,))
    assert result.code == "path_not_approved"


def test_read_rejects_binary_oversize_and_reports_truncation(tmp_path: Path) -> None:
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"a\x00b")
    large = tmp_path / "large.txt"
    large.write_text("x" * 20)
    assert read_file(str(binary), approved_roots=(tmp_path,)).code == "binary_file"
    assert (
        read_file(str(large), approved_roots=(tmp_path,), max_bytes=10).code
        == "file_too_large"
    )
    assert read_file(str(large), approved_roots=(tmp_path,), max_chars=5).truncated


def test_command_success_failure_policy_and_workdir(tmp_path: Path) -> None:
    assert run_command("pwd", [], approved_roots=(tmp_path,), cwd=str(tmp_path)).ok
    assert (
        run_command("rm", ["x"], approved_roots=(tmp_path,), cwd=str(tmp_path)).code
        == "command_forbidden"
    )
    assert (
        run_command(
            "python3", ["-c", "print(1)"], approved_roots=(tmp_path,), cwd=str(tmp_path)
        ).code
        == "command_forbidden"
    )
    assert (
        run_command("ls", ["/"], approved_roots=(tmp_path,), cwd=str(tmp_path)).code
        == "command_forbidden"
    )
    assert (
        run_command(
            "pwd", [], approved_roots=(tmp_path,), cwd=str(tmp_path.parent)
        ).code
        == "path_not_approved"
    )


def test_command_timeout_and_output_limit(tmp_path: Path, monkeypatch) -> None:
    real_run = subprocess.run

    def expire(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", expire)
    timeout = run_command(
        "git", ["log"], approved_roots=(tmp_path,), cwd=str(tmp_path), timeout=0.1
    )
    assert timeout.code == "command_timeout"
    monkeypatch.setattr(subprocess, "run", real_run)
    (tmp_path / "long-name-here").write_text("x")
    limited = run_command(
        "ls", [], approved_roots=(tmp_path,), cwd=str(tmp_path), max_output_bytes=2
    )
    assert (
        limited.ok and limited.truncated and len(limited.data["stdout"].encode()) <= 2
    )


def test_memory_session_isolation_reset_and_history_bound(tmp_path: Path) -> None:
    store = InMemoryStore(history_limit=4)
    one = store.create()
    two = store.create()
    for index in range(8):
        one.add("user", str(index))
    assert len(one.messages) == 4 and not two.messages and one.id != two.id
    assert (
        store.get(one.id) is one and store.clear(one.id) and store.get(one.id) is None
    )


def test_api_health_chat_validation_and_reset(tmp_path: Path) -> None:
    cato = Cato(
        provider=FakeModelProvider(decisions=[{"type": "final", "response": "Hello"}]),
        settings=config(tmp_path),
    )
    client = TestClient(create_app(cato))
    assert client.get("/health").json() == {"status": "ok"}
    response = client.post("/chat", json={"message": "hi"})
    assert response.status_code == 200 and response.json()["response"] == "Hello"
    session_id = response.json()["session_id"]
    assert client.delete(f"/sessions/{session_id}").json() == {"cleared": True}
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_runtime_does_not_persist_tool_payloads(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("do not retain this payload")
    provider = FakeModelProvider(
        decisions=[
            {
                "type": "tool",
                "tool": "file_read",
                "arguments": {"path": str(note)},
            },
            {"type": "final", "response": "Read."},
        ]
    )
    cato = Cato(provider=provider, settings=config(tmp_path))
    result = cato.run("read")
    session = cato.memory.get(result.session_id)
    assert session is not None
    observation = next(
        message for message in session.messages if message.role == "observation"
    )
    assert observation.content["data"] is None


def test_registry_tool_exception_remains_an_observation(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        decisions=[
            {"type": "tool", "tool": "explode", "arguments": {"value": "x"}},
            {"type": "final", "response": "Handled safely."},
        ]
    )
    cato = Cato(provider=provider, settings=config(tmp_path))
    cato.tools.register(
        ToolDefinition(
            "explode", "fails", lambda value: 1 / 0, {"value": ToolArgument(str)}
        )
    )
    assert cato.respond("explode") == "Handled safely."
