"""Conservative expected-content source patching."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from core.tool_types import ToolResult
from tools.filesystem_policy import PathOutsideApprovedRoots, resolve_approved_path


def apply_text_patch(
    path: str,
    old_text: str,
    new_text: str,
    *,
    approved_roots: tuple[Path, ...],
    expected_sha256: str | None = None,
    max_patch_bytes: int = 100_000,
) -> ToolResult:
    if len(old_text.encode()) + len(new_text.encode()) > max_patch_bytes:
        return ToolResult.failure(
            "Patch exceeds the configured size limit.", code="patch_too_large"
        )
    try:
        target = resolve_approved_path(path, approved_roots)
    except FileNotFoundError:
        return ToolResult.failure("Patch target was not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "Patch target is not approved.", code="path_not_approved"
        )
    if not target.is_file():
        return ToolResult.failure("Patch target is not a file.", code="not_file")
    dirty = _dirty_paths(target.parent)
    relative = _git_relative_path(target)
    if relative is not None and relative in dirty:
        return ToolResult.failure(
            "Patch target has pre-existing user changes.", code="dirty_worktree"
        )
    try:
        original = target.read_text()
    except (OSError, UnicodeDecodeError):
        return ToolResult.failure(
            "Patch target is not readable text.", code="read_failed"
        )
    original_hash = hashlib.sha256(original.encode()).hexdigest()
    if expected_sha256 and not __import__("hmac").compare_digest(
        expected_sha256, original_hash
    ):
        return ToolResult.failure(
            "File changed since inspection.", code="hash_mismatch"
        )
    occurrences = original.count(old_text)
    if occurrences != 1:
        return ToolResult.failure(
            "Expected source must occur exactly once.", code="patch_context_mismatch"
        )
    updated = original.replace(old_text, new_text, 1)
    mode = target.stat().st_mode
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except OSError:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        return ToolResult.failure("Patch could not be applied.", code="write_failed")
    return ToolResult.success(
        {
            "path": str(target),
            "before_sha256": original_hash,
            "after_sha256": hashlib.sha256(updated.encode()).hexdigest(),
            "lines_removed": old_text.count("\n") + 1,
            "lines_added": new_text.count("\n") + 1,
        },
        summary=f"Applied a validated patch to {target.name}.",
    )


def _git_root(path: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.decode().strip()).resolve()


def _git_relative_path(target: Path) -> str | None:
    root = _git_root(target.parent)
    return target.relative_to(root).as_posix() if root else None


def _dirty_paths(path: Path) -> set[str]:
    root = _git_root(path)
    if root is None:
        return set()
    completed = subprocess.run(
        ["git", "status", "--porcelain", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=5,
    )
    entries = completed.stdout.decode(errors="replace").split("\0")
    return {entry[3:] for entry in entries if len(entry) > 3}
