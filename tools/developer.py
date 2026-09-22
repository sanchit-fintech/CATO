"""Bounded, approval-gated project test and lint execution."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from core.tool_types import ToolResult
from tools.filesystem_policy import PathOutsideApprovedRoots, resolve_approved_path

RESULT_PATTERN = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|skipped|errors?|xfailed|xpassed)"
)


def run_project_tests(
    path: str,
    *,
    approved_roots: tuple[Path, ...],
    timeout: float = 120,
    max_output_bytes: int = 40_000,
    target: str | None = None,
) -> ToolResult:
    try:
        root = resolve_approved_path(path, approved_roots)
    except FileNotFoundError:
        return ToolResult.failure("Project not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "Project path is not approved.", code="path_not_approved"
        )
    if not root.is_dir() or not any(
        (root / marker).exists()
        for marker in ("pyproject.toml", "pytest.ini", "setup.cfg", "tests")
    ):
        return ToolResult.failure(
            "No configured Python test project was found.", code="unsupported_project"
        )
    arguments = [sys.executable, "-m", "pytest", "-q"]
    if target is not None:
        candidate = Path(target)
        if candidate.is_absolute() or ".." in candidate.parts:
            return ToolResult.failure(
                "Test target is unsafe.", code="path_not_approved"
            )
        arguments.append(target)
    return _execute(arguments, root, timeout, max_output_bytes, "Project tests")


def run_project_lint(
    path: str,
    *,
    approved_roots: tuple[Path, ...],
    timeout: float = 60,
    max_output_bytes: int = 40_000,
) -> ToolResult:
    try:
        root = resolve_approved_path(path, approved_roots)
    except FileNotFoundError:
        return ToolResult.failure("Project not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "Project path is not approved.", code="path_not_approved"
        )
    if not (root / "pyproject.toml").exists():
        return ToolResult.failure(
            "Ruff is not configured here.", code="unsupported_project"
        )
    return _execute(
        [sys.executable, "-m", "ruff", "check", "."],
        root,
        timeout,
        max_output_bytes,
        "Project lint",
    )


def _execute(
    arguments: list[str], root: Path, timeout: float, max_output: int, label: str
) -> ToolResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            arguments,
            cwd=root,
            capture_output=True,
            timeout=max(1, min(timeout, 600)),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or b"") + (error.stderr or b"")
        return ToolResult.failure(
            f"{label} timed out after producing {min(len(output), max_output)} bytes.",
            code="command_timeout",
        )
    except OSError:
        return ToolResult.failure(f"{label} could not start.", code="command_error")
    output = completed.stdout + completed.stderr
    text = output[:max_output].decode(errors="replace")
    counts: dict[str, int] = {}
    for match in RESULT_PATTERN.finditer(text):
        counts[match.group("kind")] = int(match.group("count"))
    duration = time.monotonic() - started
    return ToolResult.success(
        {
            "return_code": completed.returncode,
            "results": counts,
            "output": text,
            "duration_seconds": duration,
        },
        summary=f"{label} exited with code {completed.returncode}.",
        truncated=len(output) > max_output,
        metadata={"duration_ms": duration * 1000},
    )
