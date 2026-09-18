"""Bounded filename search within approved filesystem roots."""

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

    matches: list[str] = []
    scanned = 0
    skipped_sensitive = 0
    for root_path in search_roots:
        try:
            for path in root_path.rglob("*"):
                scanned += 1
                if scanned > max_entries or len(matches) >= max_results:
                    break
                if query in path.name.lower():
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
