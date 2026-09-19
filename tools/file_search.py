"""Bounded filename search within approved filesystem roots."""

from datetime import datetime
from pathlib import Path

from core.tool_types import ToolResult
from tools.filesystem_policy import (
    PathOutsideApprovedRoots,
    is_sensitive_path,
    resolve_approved_path,
)


def search_files(
    query: str,
    *,
    approved_roots: tuple[Path, ...],
    root: str | None = None,
    max_results: int = 50,
    max_entries: int = 50_000,
    extension: str | None = None,
    modified_after: str | None = None,
) -> ToolResult:
    query = query.lower().strip()
    if not query:
        return ToolResult.failure("A search query is required.", code="invalid_query")

    try:
        search_roots = (
            (resolve_approved_path(root, approved_roots),)
            if root is not None
            else approved_roots
        )
    except FileNotFoundError:
        return ToolResult.failure("Search root not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "That path is outside Cato's approved roots.", code="path_not_approved"
        )

    try:
        cutoff = (
            datetime.fromisoformat(modified_after).timestamp()
            if modified_after
            else None
        )
    except ValueError:
        return ToolResult.failure(
            "modified_after must be an ISO date or timestamp.", code="invalid_arguments"
        )
    normalized_extension = extension.lower().lstrip(".") if extension else None
    matches: list[str] = []
    scanned = 0
    skipped_sensitive = 0
    for root_path in search_roots:
        try:
            for path in root_path.rglob("*"):
                scanned += 1
                if scanned > max_entries or len(matches) >= max_results:
                    break
                if (
                    query in path.name.lower()
                    and (
                        not normalized_extension
                        or path.suffix.lower().lstrip(".") == normalized_extension
                    )
                    and (cutoff is None or path.stat().st_mtime >= cutoff)
                ):
                    if is_sensitive_path(path):
                        skipped_sensitive += 1
                    else:
                        try:
                            resolve_approved_path(path, approved_roots)
                        except (FileNotFoundError, PathOutsideApprovedRoots, OSError):
                            continue
                        matches.append(str(path))
        except (PermissionError, OSError):
            continue
        if scanned > max_entries or len(matches) >= max_results:
            break

    return ToolResult.success(
        {
            "matches": matches,
            "skipped_sensitive": skipped_sensitive,
            "truncated": scanned > max_entries or len(matches) >= max_results,
        }
    )
