from pathlib import Path

import pytest

from core.tool_registry import ToolRegistry
from core.tool_types import ToolArgument, ToolDefinition, ToolResult
from tools.file_read import read_file
from tools.file_search import search_files


def test_registry_registration_dispatch_and_metadata() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo text.",
            function=lambda text: text,
            arguments={"text": ToolArgument(str)},
        )
    )
    assert registry.list_tools() == ["echo"]
    assert registry.get("echo").description == "Echo text."
    assert registry.run("echo", text="hello").data == "hello"


def test_registry_rejects_duplicate_and_invalid_arguments() -> None:
    registry = ToolRegistry()
    tool = ToolDefinition(
        "echo", "Echo.", lambda text: text, {"text": ToolArgument(str)}
    )
    registry.register(tool)
    with pytest.raises(ValueError):
        registry.register(tool)
    assert registry.run("echo").code == "invalid_arguments"
    assert registry.run("echo", text=3).code == "invalid_arguments"
    assert registry.run("echo", text="x", extra=True).code == "invalid_arguments"
    assert registry.run("missing").code == "unknown_tool"


def test_file_read_and_search(tmp_path: Path) -> None:
    file = tmp_path / "Example.txt"
    file.write_text("hello", encoding="utf-8")
    read = read_file(str(file), approved_roots=(tmp_path,))
    search = search_files("example", approved_roots=(tmp_path,))
    assert read.ok and read.data["content"] == "hello"
    assert search.ok and search.data["matches"] == [str(file)]


def test_missing_file_and_directory_read(tmp_path: Path) -> None:
    assert (
        read_file(str(tmp_path / "missing"), approved_roots=(tmp_path,)).code
        == "not_found"
    )
    assert read_file(str(tmp_path), approved_roots=(tmp_path,)).code == "not_file"


def test_outside_root_and_traversal_are_denied(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    direct = read_file(str(outside), approved_roots=(approved,))
    traversal = read_file(
        str(approved / ".." / "outside.txt"), approved_roots=(approved,)
    )
    assert direct.code == traversal.code == "path_not_approved"


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    link = approved / "link.txt"
    link.symlink_to(outside)
    result = read_file(str(link), approved_roots=(approved,))
    assert result.code == "path_not_approved"

    search = search_files("link", approved_roots=(approved,))
    assert search.data["matches"] == []


@pytest.mark.parametrize("name", [".env", "id_rsa", "access_token.txt", "private.pem"])
def test_sensitive_files_require_approval(tmp_path: Path, name: str) -> None:
    file = tmp_path / name
    file.write_text("do not expose")
    result = read_file(str(file), approved_roots=(tmp_path,))
    assert not result.ok
    assert result.requires_approval
    assert result.data is None


def test_search_hides_sensitive_matches(tmp_path: Path) -> None:
    (tmp_path / "token_notes.txt").write_text("secret")
    result = search_files("token", approved_roots=(tmp_path,))
    assert result.data["matches"] == []
    assert result.data["skipped_sensitive"] == 1


def test_tool_exception_is_structured() -> None:
    registry = ToolRegistry()

    def fail() -> ToolResult:
        raise PermissionError

    registry.register(ToolDefinition("fail", "Fail.", fail, {}))
    assert registry.run("fail").code == "tool_error"
