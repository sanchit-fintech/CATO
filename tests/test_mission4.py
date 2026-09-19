from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cato_platform.macos import MacOSController, is_macos
from core.api import create_app
from core.approvals import ApprovalStore
from core.cato import Cato
from core.config import Settings
from core.llm.fake import FakeModelProvider


class FakeRunner:
    def __init__(self, *, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, b"")


def settings(root: Path, *, ttl: int = 300, clipboard: int = 100_000) -> Settings:
    return Settings(
        None,
        "fake",
        (root,),
        "CRITICAL",
        approval_ttl_seconds=ttl,
        max_clipboard_bytes=clipboard,
    )


def controller(tmp_path: Path, runner: FakeRunner, *, limit: int = 100_000):
    return MacOSController(
        (tmp_path,),
        frozenset({"Finder", "Safari", "Terminal", "Visual Studio Code", "Notes"}),
        limit,
        runner,
    )


def test_platform_detection_and_conditional_registration(tmp_path: Path) -> None:
    assert is_macos("Darwin") and not is_macos("Linux")
    provider = FakeModelProvider()
    linux = Cato(
        provider=provider, settings=settings(tmp_path), platform_system="Linux"
    )
    assert "mac_open_app" not in linux.tools.list_tools()
    mac = Cato(
        provider=provider,
        settings=settings(tmp_path),
        macos=controller(tmp_path, FakeRunner()),
        platform_system="Darwin",
    )
    assert "mac_open_app" in mac.tools.list_tools()


@pytest.mark.parametrize("name", ["Safari; rm -rf /", "../../Safari", "Unknown"])
def test_open_app_rejects_invalid_or_unapproved_names(
    tmp_path: Path, name: str
) -> None:
    runner = FakeRunner()
    result = controller(tmp_path, runner).open_app(name)
    assert result.code == "app_not_allowed" and not runner.calls


def test_open_and_activate_allowed_app_use_argv(tmp_path: Path) -> None:
    runner = FakeRunner()
    mac = controller(tmp_path, runner)
    assert mac.open_app("vs code").ok
    assert mac.activate_app("Safari").ok
    assert runner.calls[0][0] == ["/usr/bin/open", "-a", "Visual Studio Code"]
    assert runner.calls[1][0] == ["/usr/bin/open", "-a", "Safari"]


def test_open_file_folder_reveal_and_path_boundaries(tmp_path: Path) -> None:
    runner = FakeRunner()
    mac = controller(tmp_path, runner)
    folder = tmp_path / "project"
    folder.mkdir()
    file = folder / "note.txt"
    file.write_text("hello")
    assert mac.open_path(str(file), directory=False).ok
    assert mac.open_path(str(folder), directory=True).ok
    assert mac.open_path(str(file), reveal=True).ok
    assert runner.calls[-1][0] == ["/usr/bin/open", "-R", str(file)]
    assert mac.open_path(str(tmp_path / "missing")).code == "not_found"
    outside = tmp_path.parent / "outside-mission4.txt"
    outside.write_text("outside")
    try:
        assert mac.open_path(str(outside)).code == "path_not_approved"
        link = tmp_path / "escape"
        link.symlink_to(outside)
        assert mac.open_path(str(link)).code == "path_not_approved"
    finally:
        outside.unlink()


@pytest.mark.parametrize("url", ["https://example.com/a?q=1", "http://localhost:8000"])
def test_safe_urls_are_opened(tmp_path: Path, url: str) -> None:
    runner = FakeRunner()
    assert controller(tmp_path, runner).open_url(url).ok
    assert runner.calls[0][0] == ["/usr/bin/open", url]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/plain,x",
        "not a url",
        "https://user:pass@example.com",
    ],
)
def test_unsafe_or_malformed_urls_are_rejected(tmp_path: Path, url: str) -> None:
    runner = FakeRunner()
    assert not controller(tmp_path, runner).open_url(url).ok
    assert not runner.calls


def test_clipboard_text_read_write_and_limits(tmp_path: Path, caplog) -> None:
    read_runner = FakeRunner(stdout=b"clipboard secret")
    read = controller(tmp_path, read_runner, limit=20).clipboard_read()
    assert read.data == {"text": "clipboard secret"}
    write_runner = FakeRunner()
    write = controller(tmp_path, write_runner, limit=20).clipboard_write("hello")
    assert write.ok and write_runner.calls[0][1] == b"hello"
    assert (
        controller(tmp_path, FakeRunner(stdout=b"x" * 21), limit=20)
        .clipboard_read()
        .code
        == "clipboard_too_large"
    )
    assert (
        controller(tmp_path, FakeRunner(), limit=2).clipboard_write("hello").code
        == "clipboard_too_large"
    )
    assert "clipboard secret" not in caplog.text


def test_notification_special_text_is_never_script(tmp_path: Path) -> None:
    runner = FakeRunner()
    title = 'Title "quoted"'
    message = 'hello\nend run\ndo shell script "bad"'
    assert controller(tmp_path, runner).show_notification(title, message).ok
    argv = runner.calls[0][0]
    assert title in argv and message in argv
    assert all(message not in item for item in argv[: argv.index("--")])


def test_running_apps_returns_names_only(tmp_path: Path) -> None:
    result = controller(
        tmp_path, FakeRunner(stdout=b"Safari, Finder, Safari\n")
    ).list_running_apps()
    assert result.data == {"applications": ["Finder", "Safari"]}


def test_vscode_terminal_and_permission_status(tmp_path: Path, monkeypatch) -> None:
    runner = FakeRunner()
    mac = controller(tmp_path, runner)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert mac.open_in_vscode(str(project)).ok
    assert runner.calls[-1][0][:3] == ["/usr/bin/open", "-a", "Visual Studio Code"]
    assert mac.open_in_terminal(str(project)).ok
    status = mac.permission_status()
    assert status.ok and status.data["accessibility"] == "unknown"


def test_approval_store_session_expiry_denial_and_replay() -> None:
    store = ApprovalStore(ttl_seconds=60)
    pending = store.create(
        session_id="one",
        tool="x",
        arguments={},
        request="x",
        summary="X",
        risk="moderate",
    )
    assert store.approve(pending.id, "two").code == "approval_session_mismatch"
    assert store.deny(pending.id, "one").ok
    assert store.approve(pending.id, "one").code == "approval_denied"
    expired = store.create(
        session_id="one",
        tool="x",
        arguments={},
        request="x",
        summary="X",
        risk="moderate",
    )
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert store.approve(expired.id, "one").code == "approval_expired"


def test_invalid_approval_action_arguments_do_not_create_approval(
    tmp_path: Path,
) -> None:
    provider = FakeModelProvider(
        decisions=[
            {"type": "tool", "tool": "mac_quit_app", "arguments": {}},
            {"type": "final", "response": "Could not quit."},
        ]
    )
    cato = Cato(
        provider=provider,
        settings=settings(tmp_path),
        macos=controller(tmp_path, FakeRunner()),
        platform_system="Darwin",
    )
    result = cato.run("quit")
    assert result.status == "completed" and result.approval_id is None


def test_quit_pauses_then_approval_executes_once_and_continues(tmp_path: Path) -> None:
    runner = FakeRunner()
    provider = FakeModelProvider(
        decisions=[
            {"type": "tool", "tool": "mac_quit_app", "arguments": {"name": "Safari"}},
            {"type": "final", "response": "Safari was quit."},
        ]
    )
    cato = Cato(
        provider=provider,
        settings=settings(tmp_path),
        macos=controller(tmp_path, runner),
        platform_system="Darwin",
    )
    paused = cato.run("quit Safari")
    assert paused.status == "approval_required" and paused.approval_id
    assert not runner.calls
    completed = cato.approve(paused.approval_id, paused.session_id)
    assert completed.status == "completed" and completed.response == "Safari was quit."
    assert len(runner.calls) == 1
    replay = cato.approve(paused.approval_id, paused.session_id)
    assert replay.status == "approval_used" and len(runner.calls) == 1


def test_wrong_session_and_denial_never_execute(tmp_path: Path) -> None:
    runner = FakeRunner()
    provider = FakeModelProvider(
        decisions=[
            {
                "type": "tool",
                "tool": "mac_clipboard_write",
                "arguments": {"text": "secret"},
            }
        ]
    )
    cato = Cato(
        provider=provider,
        settings=settings(tmp_path),
        macos=controller(tmp_path, runner),
        platform_system="Darwin",
    )
    paused = cato.run("copy this")
    other = cato.memory.create()
    assert (
        cato.approve(paused.approval_id, other.id).status == "approval_session_mismatch"
    )
    assert cato.deny(paused.approval_id, paused.session_id).status == "denied"
    assert not runner.calls


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "file_move",
            {"source": "source.txt", "destination": "destination.txt"},
        ),
        (
            "file_write",
            {"path": "source.txt", "content": "replacement", "overwrite": True},
        ),
    ],
)
def test_existing_moderate_file_actions_pause_before_execution(
    tmp_path: Path, tool: str, arguments: dict[str, object]
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("original")
    arguments = {
        key: str(tmp_path / value)
        if key in {"source", "destination", "path"}
        else value
        for key, value in arguments.items()
    }
    provider = FakeModelProvider(
        decisions=[{"type": "tool", "tool": tool, "arguments": arguments}]
    )
    cato = Cato(provider=provider, settings=settings(tmp_path), platform_system="Linux")
    paused = cato.run("change file")
    assert paused.status == "approval_required"
    assert source.read_text() == "original"


def test_api_approval_routes(tmp_path: Path) -> None:
    runner = FakeRunner()
    provider = FakeModelProvider(
        decisions=[
            {"type": "tool", "tool": "mac_quit_app", "arguments": {"name": "Notes"}},
            {"type": "final", "response": "Done."},
        ]
    )
    cato = Cato(
        provider=provider,
        settings=settings(tmp_path),
        macos=controller(tmp_path, runner),
        platform_system="Darwin",
    )
    client = TestClient(create_app(cato))
    paused = client.post("/chat", json={"message": "quit Notes"}).json()
    assert paused["status"] == "approval_required" and paused["risk"] == "moderate"
    approved = client.post(
        f"/approvals/{paused['approval_id']}/approve",
        json={"session_id": paused["session_id"]},
    ).json()
    assert approved["status"] == "completed"
    invalid = client.post(
        "/approvals/not-real/deny", json={"session_id": paused["session_id"]}
    ).json()
    assert invalid["status"] == "approval_not_found"
