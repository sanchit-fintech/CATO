from pathlib import Path

from core.tool_types import ToolResult
from tools.filesystem_policy import (
    PathOutsideApprovedRoots,
    is_sensitive_path,
    resolve_approved_path,
)


def read_file(
    path: str,
    *,
    approved_roots: tuple[Path, ...],
    max_chars: int = 12_000,
    max_bytes: int = 1_000_000,
) -> ToolResult:
    """
    Read a text file from the user's Mac.
    """

    try:
        file_path = resolve_approved_path(path, approved_roots)
    except FileNotFoundError:
        return ToolResult.failure("File not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "That path is outside Cato's approved roots.", code="path_not_approved"
        )

    if not file_path.is_file():
        return ToolResult.failure("The requested path is not a file.", code="not_file")
    if is_sensitive_path(file_path):
        return ToolResult.failure(
            "This file may contain sensitive information and requires "
            "explicit approval.",
            code="approval_required",
            requires_approval=True,
        )

    try:
        size = file_path.stat().st_size
        if size > max_bytes:
            return ToolResult.failure(
                "The file is too large to read safely.", code="file_too_large"
            )
        sample = file_path.read_bytes()[:8192]
        if b"\x00" in sample:
            return ToolResult.failure(
                "Binary files cannot be read as text.", code="binary_file"
            )
    except OSError:
        return ToolResult.failure("The file could not be read.", code="read_error")

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult.failure(
            "Binary or non-UTF-8 files cannot be read as text.", code="binary_file"
        )
    except PermissionError:
        return ToolResult.failure("Permission denied.", code="permission_denied")
    except OSError:
        return ToolResult.failure("The file could not be read.", code="read_error")

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
        content += "\n\n[Output truncated.]"

    return ToolResult.success(
        {"path": str(file_path), "content": content},
        summary="File read.",
        truncated=truncated,
    )
