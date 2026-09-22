"""Deterministic project discovery and safe developer inspection tools."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from core.tool_types import ToolResult
from tools.filesystem_policy import PathOutsideApprovedRoots, resolve_approved_path

PROJECT_MARKERS = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "package.json": "node",
    "Cargo.toml": "rust",
    "go.mod": "go",
}
IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}


def discover_projects(
    root: str,
    *,
    approved_roots: tuple[Path, ...],
    query: str | None = None,
    max_depth: int = 5,
    max_results: int = 30,
) -> ToolResult:
    try:
        base = resolve_approved_path(root, approved_roots)
    except FileNotFoundError:
        return ToolResult.failure("Project search root not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "Project root is not approved.", code="path_not_approved"
        )
    if not base.is_dir():
        return ToolResult.failure(
            "Project search root is not a directory.", code="not_directory"
        )
    found: dict[Path, set[str]] = {}
    scanned = 0
    for path in base.rglob("*"):
        relative = path.relative_to(base)
        if len(relative.parts) > max_depth or any(
            part in IGNORED for part in relative.parts
        ):
            continue
        scanned += 1
        if path.name in PROJECT_MARKERS:
            found.setdefault(path.parent, set()).add(PROJECT_MARKERS[path.name])
        if path.name == ".git" and path.is_dir():
            found.setdefault(path.parent, set()).add("git")
        if len(found) >= max_results:
            break
    normalized = query.lower() if query else None
    projects = [
        {"path": str(path), "name": path.name, "types": sorted(types)}
        for path, types in found.items()
        if normalized is None or normalized in path.name.lower()
    ]
    projects.sort(
        key=lambda item: (
            normalized not in item["name"].lower() if normalized else False,
            item["name"].lower(),
        )
    )
    return ToolResult.success(
        {"projects": projects[:max_results], "scanned": scanned},
        summary=f"Found {len(projects[:max_results])} projects.",
        truncated=len(projects) > max_results,
    )


def inspect_project(path: str, *, approved_roots: tuple[Path, ...]) -> ToolResult:
    try:
        root = resolve_approved_path(path, approved_roots)
    except FileNotFoundError:
        return ToolResult.failure("Project not found.", code="not_found")
    except (PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "Project path is not approved.", code="path_not_approved"
        )
    if not root.is_dir():
        return ToolResult.failure(
            "Project path is not a directory.", code="not_directory"
        )
    markers = [name for name in PROJECT_MARKERS if (root / name).exists()]
    types = sorted({PROJECT_MARKERS[name] for name in markers})
    if (root / ".git").exists():
        types.append("git")
    data = {
        "path": str(root),
        "name": root.name,
        "types": types,
        "markers": markers,
        "test_command": _test_command(types, root),
        "major_directories": sorted(
            child.name
            for child in root.iterdir()
            if child.is_dir()
            and child.name not in IGNORED
            and not child.name.startswith(".")
        )[:30],
    }
    if "git" in types:
        data["git"] = _git_snapshot(root)
    return ToolResult.success(data, summary=f"Inspected {root.name}.")


def git_inspect(
    path: str, *, approved_roots: tuple[Path, ...], max_output: int = 20_000
) -> ToolResult:
    project = inspect_project(path, approved_roots=approved_roots)
    if not project.ok:
        return project
    root = Path(project.data["path"])
    if not (root / ".git").exists():
        return ToolResult.failure(
            "The path is not a Git repository.", code="not_git_repository"
        )
    started = time.monotonic()
    snapshot = _git_snapshot(root, max_output=max_output)
    return ToolResult.success(
        snapshot,
        summary="Git repository inspected.",
        metadata={"duration_ms": (time.monotonic() - started) * 1000},
    )


def _git_snapshot(root: Path, *, max_output: int = 20_000) -> dict[str, str]:
    def run(arguments: list[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=root,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        return completed.stdout[:max_output].decode(errors="replace").strip()

    return {
        "branch": run(["branch", "--show-current"]),
        "status": run(["status", "--short"]),
        "recent_commits": run(["log", "-5", "--pretty=format:%h %s"]),
    }


def _test_command(types: list[str], root: Path) -> list[str] | None:
    if "python" in types:
        return ["python", "-m", "pytest"]
    if "node" in types and (root / "package.json").exists():
        return ["npm", "test"]
    if "rust" in types:
        return ["cargo", "test"]
    if "go" in types:
        return ["go", "test", "./..."]
    return None
