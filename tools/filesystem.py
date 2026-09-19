"""Bounded local filesystem primitives."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from core.tool_types import ToolResult
from tools.filesystem_policy import (
    PathOutsideApprovedRoots,
    is_sensitive_path,
    resolve_approved_path,
)


def _path(
    path: str, roots: tuple[Path, ...], *, exists: bool = True
) -> Path | ToolResult:
    try:
        return resolve_approved_path(path, roots, must_exist=exists)
    except FileNotFoundError:
        return ToolResult.failure("Path not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "That path is outside Cato's approved roots.", code="path_not_approved"
        )


def _metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "type": "directory" if path.is_dir() else "file",
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "extension": path.suffix.lower(),
    }


def list_files(
    path: str,
    *,
    approved_roots: tuple[Path, ...],
    recursive: bool = False,
    max_depth: int = 3,
    max_results: int = 200,
    include_hidden: bool = False,
) -> ToolResult:
    root = _path(path, approved_roots)
    if isinstance(root, ToolResult):
        return root
    if not root.is_dir():
        return ToolResult.failure(
            "The requested path is not a directory.", code="not_directory"
        )
    max_depth = max(0, min(max_depth, 10))
    entries: list[dict[str, Any]] = []
    try:
        candidates = root.rglob("*") if recursive else root.iterdir()
        for item in candidates:
            relative = item.relative_to(root)
            if len(relative.parts) > max_depth or (
                not include_hidden and any(p.startswith(".") for p in relative.parts)
            ):
                continue
            if is_sensitive_path(item):
                continue
            try:
                safe = resolve_approved_path(item, approved_roots)
                entries.append(_metadata(safe))
            except (OSError, PathOutsideApprovedRoots, FileNotFoundError):
                continue
            if len(entries) >= max_results:
                return ToolResult.success(
                    entries, summary=f"Listed {len(entries)} entries.", truncated=True
                )
    except (PermissionError, OSError):
        return ToolResult.failure(
            "The directory could not be listed.", code="read_error"
        )
    return ToolResult.success(entries, summary=f"Listed {len(entries)} entries.")


def stat_file(path: str, *, approved_roots: tuple[Path, ...]) -> ToolResult:
    target = _path(path, approved_roots)
    if isinstance(target, ToolResult):
        return target
    if is_sensitive_path(target):
        return ToolResult.failure(
            "This path is sensitive and requires explicit approval.",
            code="approval_required",
            requires_approval=True,
        )
    return ToolResult.success(_metadata(target), summary="Metadata retrieved.")


def create_directory(
    path: str, *, approved_roots: tuple[Path, ...], parents: bool = False
) -> ToolResult:
    target = _path(path, approved_roots, exists=False)
    if isinstance(target, ToolResult):
        return target
    if target.exists():
        return ToolResult.failure(
            "The destination already exists.", code="already_exists"
        )
    try:
        target.mkdir(parents=parents, exist_ok=False)
    except FileNotFoundError:
        return ToolResult.failure("Parent directory not found.", code="not_found")
    except OSError:
        return ToolResult.failure("Directory could not be created.", code="write_error")
    return ToolResult.success({"path": str(target)}, summary="Directory created.")


def write_file(
    path: str,
    content: str,
    *,
    approved_roots: tuple[Path, ...],
    overwrite: bool = False,
) -> ToolResult:
    target = _path(path, approved_roots, exists=False)
    if isinstance(target, ToolResult):
        return target
    if is_sensitive_path(target):
        return ToolResult.failure(
            "Writing sensitive files is not allowed.", code="sensitive_path"
        )
    if target.exists() and not overwrite:
        return ToolResult.failure(
            "The destination already exists; set overwrite explicitly.",
            code="already_exists",
        )
    if not target.parent.exists():
        return ToolResult.failure("Parent directory not found.", code="not_found")
    try:
        target.write_text(content, encoding="utf-8")
    except OSError:
        return ToolResult.failure("File could not be written.", code="write_error")
    return ToolResult.success(
        {"path": str(target), "bytes": len(content.encode())}, summary="File written."
    )


def _transfer(
    source: str,
    destination: str,
    *,
    approved_roots: tuple[Path, ...],
    overwrite: bool,
    move: bool,
) -> ToolResult:
    src = _path(source, approved_roots)
    dst = _path(destination, approved_roots, exists=False)
    if isinstance(src, ToolResult):
        return src
    if isinstance(dst, ToolResult):
        return dst
    if is_sensitive_path(src) or is_sensitive_path(dst):
        return ToolResult.failure(
            "Sensitive paths cannot be transferred.", code="sensitive_path"
        )
    if src.is_dir():
        try:
            for child in src.rglob("*"):
                if is_sensitive_path(child):
                    return ToolResult.failure(
                        "Directories containing sensitive paths cannot be transferred.",
                        code="sensitive_path",
                    )
                resolve_approved_path(child, approved_roots)
        except (OSError, PathOutsideApprovedRoots, FileNotFoundError):
            return ToolResult.failure(
                "The directory contains an unsafe link or unreadable path.",
                code="path_not_approved",
            )
    if dst.exists() and not overwrite:
        return ToolResult.failure(
            "The destination already exists.", code="already_exists"
        )
    if not dst.parent.exists():
        return ToolResult.failure("Destination parent not found.", code="not_found")
    try:
        if dst.exists() and overwrite and dst.is_dir():
            return ToolResult.failure(
                "Directory overwrite is not supported.", code="unsafe_overwrite"
            )
        if move:
            shutil.move(str(src), str(dst))
        elif src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=overwrite)
        else:
            shutil.copy2(src, dst)
    except OSError:
        return ToolResult.failure(
            "Transfer could not be completed.", code="write_error"
        )
    return ToolResult.success(
        {"source": str(src), "destination": str(dst)},
        summary="Path moved." if move else "Path copied.",
    )


def copy_path(
    source: str,
    destination: str,
    *,
    approved_roots: tuple[Path, ...],
    overwrite: bool = False,
) -> ToolResult:
    return _transfer(
        source,
        destination,
        approved_roots=approved_roots,
        overwrite=overwrite,
        move=False,
    )


def move_path(
    source: str,
    destination: str,
    *,
    approved_roots: tuple[Path, ...],
    overwrite: bool = False,
) -> ToolResult:
    return _transfer(
        source,
        destination,
        approved_roots=approved_roots,
        overwrite=overwrite,
        move=True,
    )
