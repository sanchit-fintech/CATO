"""Structured command execution with a deliberately narrow allowlist."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.tool_types import ToolResult
from tools.filesystem_policy import PathOutsideApprovedRoots, resolve_approved_path

ALLOWED = {"pwd", "ls", "git", "python", "python3"}
SAFE_GIT = {"status", "diff", "log", "show", "branch"}


def run_command(
    command: str,
    arguments: list[str],
    *,
    approved_roots: tuple[Path, ...],
    cwd: str,
    timeout: float = 10,
    max_output_bytes: int = 20_000,
) -> ToolResult:
    if (
        command not in ALLOWED
        or command == "git"
        and (not arguments or arguments[0] not in SAFE_GIT)
    ):
        return ToolResult.failure(
            "That command is not allowed by policy.",
            code="command_forbidden",
            requires_approval=True,
        )
    if not isinstance(arguments, list) or not all(
        isinstance(arg, str) for arg in arguments
    ):
        return ToolResult.failure(
            "Command arguments must be a string list.", code="invalid_arguments"
        )
    if command in {"python", "python3"} and arguments not in (["--version"], ["-V"]):
        return ToolResult.failure(
            "Only Python version inspection is allowed.",
            code="command_forbidden",
            requires_approval=True,
        )
    if command == "pwd" and arguments:
        return ToolResult.failure(
            "pwd does not accept arguments here.", code="command_forbidden"
        )
    if command == "ls" and any(
        Path(arg).is_absolute() or ".." in Path(arg).parts
        for arg in arguments
        if not arg.startswith("-")
    ):
        return ToolResult.failure(
            "Command paths must stay within the working directory.",
            code="command_forbidden",
        )
    try:
        workdir = resolve_approved_path(cwd, approved_roots)
    except (FileNotFoundError, PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "Working directory is not approved.", code="path_not_approved"
        )
    if not workdir.is_dir():
        return ToolResult.failure(
            "Working directory is not a directory.", code="not_directory"
        )
    try:
        completed = subprocess.run(
            [command, *arguments],
            cwd=workdir,
            capture_output=True,
            timeout=max(0.1, min(timeout, 60)),
            shell=False,
            check=False,
        )
    except FileNotFoundError:
        return ToolResult.failure(
            "Command executable was not found.", code="command_not_found"
        )
    except subprocess.TimeoutExpired:
        return ToolResult.failure("Command timed out.", code="command_timeout")
    except OSError:
        return ToolResult.failure(
            "Command could not be executed.", code="command_error"
        )
    stdout, stderr = completed.stdout, completed.stderr
    truncated = len(stdout) + len(stderr) > max_output_bytes
    remaining = max_output_bytes
    out = stdout[:remaining]
    remaining -= len(out)
    err = stderr[:remaining]
    data = {
        "return_code": completed.returncode,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
    }
    return ToolResult.success(
        data,
        summary=f"Command exited with code {completed.returncode}.",
        truncated=truncated,
    )
