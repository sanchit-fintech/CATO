"""Atomic multi-file coding transactions with checkpoints and safe rollback."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import shutil
import stat
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.tool_types import ToolResult
from tools.filesystem_policy import PathOutsideApprovedRoots, resolve_approved_path
from tools.patch import _dirty_paths, _git_relative_path


@dataclass(frozen=True)
class SourceCheckpoint:
    path: str
    before_sha256: str | None
    after_sha256: str
    mode: int
    backup: str | None
    created: bool


def apply_patch_set(
    workspace: str,
    task_id: str,
    operations: list[dict[str, Any]],
    *,
    approved_roots: tuple[Path, ...],
    max_files: int = 10,
    max_patch_bytes: int = 250_000,
    artifact_quota_bytes: int = 5_000_000,
    dry_run: bool = False,
) -> ToolResult:
    try:
        root = resolve_approved_path(workspace, approved_roots)
    except (FileNotFoundError, PathOutsideApprovedRoots, OSError):
        return ToolResult.failure(
            "Workspace is not approved.", code="path_not_approved"
        )
    if not root.is_dir() or not task_id or len(task_id) > 128:
        return ToolResult.failure(
            "Invalid coding transaction.", code="invalid_transaction"
        )
    if not operations or len(operations) > max_files:
        return ToolResult.failure(
            "Patch set has an invalid file count.", code="patch_budget"
        )
    if len(json.dumps(operations).encode()) > max_patch_bytes:
        return ToolResult.failure(
            "Patch set exceeds its byte budget.", code="patch_budget"
        )
    proposed: list[tuple[Path, str | None, str, int]] = []
    seen: set[Path] = set()
    for operation in operations:
        relative = operation.get("path")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            return ToolResult.failure("Patch path is unsafe.", code="path_not_approved")
        target = (root / relative).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError:
            return ToolResult.failure(
                "Patch escapes workspace.", code="path_not_approved"
            )
        if target in seen:
            return ToolResult.failure(
                "Only one operation per file is allowed.", code="duplicate_target"
            )
        seen.add(target)
        kind = operation.get("kind")
        original: str | None
        if kind == "create":
            if target.exists():
                return ToolResult.failure(
                    "Create target already exists.", code="target_exists"
                )
            original, updated, mode = None, operation.get("text"), 0o600
        else:
            if not target.is_file():
                return ToolResult.failure("Patch target is missing.", code="not_found")
            relative_git = _git_relative_path(target)
            if relative_git and relative_git in _dirty_paths(target.parent):
                return ToolResult.failure(
                    "Target has user changes.", code="dirty_worktree"
                )
            try:
                original = target.read_text()
            except (OSError, UnicodeDecodeError):
                return ToolResult.failure(
                    "Target is not readable text.", code="read_failed"
                )
            digest = hashlib.sha256(original.encode()).hexdigest()
            if operation.get("expected_sha256") != digest:
                return ToolResult.failure("Source hash is stale.", code="hash_mismatch")
            anchor = operation.get("old_text") or operation.get("anchor")
            text = operation.get("new_text") or operation.get("text")
            if (
                not isinstance(anchor, str)
                or not isinstance(text, str)
                or original.count(anchor) != 1
            ):
                return ToolResult.failure(
                    "Patch context is ambiguous.", code="patch_context_mismatch"
                )
            if kind == "replace":
                updated = original.replace(anchor, text, 1)
            elif kind == "insert_before":
                updated = original.replace(anchor, text + anchor, 1)
            elif kind == "insert_after":
                updated = original.replace(anchor, anchor + text, 1)
            else:
                return ToolResult.failure(
                    "Unknown patch operation.", code="invalid_operation"
                )
            mode = stat.S_IMODE(target.stat().st_mode)
        if not isinstance(updated, str):
            return ToolResult.failure(
                "Patch text is invalid.", code="invalid_operation"
            )
        validation = _validate_source(target, updated)
        if validation:
            return ToolResult.failure(validation, code="source_invalid")
        proposed.append((target, original, updated, mode))
    preview = [
        {"path": str(path), "before": _hash(old), "after": _hash(new)}
        for path, old, new, _ in proposed
    ]
    if dry_run:
        return ToolResult.success(
            {"dry_run": True, "changes": preview},
            summary="Patch set validated without mutation.",
        )
    artifact = root / ".cato" / "artifacts" / task_id / "checkpoint"
    artifact.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoints: list[SourceCheckpoint] = []
    diff_parts: list[str] = []
    insertions = 0
    deletions = 0
    total = sum(len((old or "").encode()) for _, old, _, _ in proposed)
    if total > artifact_quota_bytes:
        return ToolResult.failure(
            "Checkpoint exceeds artifact quota.", code="artifact_quota"
        )
    for index, (target, old, new, mode) in enumerate(proposed):
        backup = None
        if old is not None:
            backup_path = artifact / f"{index:04d}.bak"
            backup_path.write_text(old)
            os.chmod(backup_path, 0o600)
            backup = str(backup_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False
        )
        try:
            temporary.write(new)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            os.chmod(temporary.name, mode)
            os.replace(temporary.name, target)
        except OSError:
            temporary.close()
            Path(temporary.name).unlink(missing_ok=True)
            _rollback(checkpoints)
            return ToolResult.failure(
                "Atomic patch application failed.", code="apply_failed"
            )
        checkpoints.append(
            SourceCheckpoint(
                str(target), _hash(old), _hash(new), mode, backup, old is None
            )
        )
        relative = target.relative_to(root).as_posix()
        lines = list(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{relative}" if old is not None else "/dev/null",
                tofile=f"b/{relative}",
            )
        )
        diff_parts.extend(lines)
        insertions += sum(
            1 for line in lines if line.startswith("+") and not line.startswith("+++")
        )
        deletions += sum(
            1 for line in lines if line.startswith("-") and not line.startswith("---")
        )
    metadata = root / ".cato" / "artifacts" / task_id / "transaction.json"
    metadata.write_text(json.dumps([asdict(item) for item in checkpoints], indent=2))
    os.chmod(metadata, 0o600)
    diff_path = root / ".cato" / "artifacts" / task_id / "changes.diff"
    diff_path.write_text("".join(diff_parts))
    os.chmod(diff_path, 0o600)
    return ToolResult.success(
        {
            "task_id": task_id,
            "changes": [asdict(item) for item in checkpoints],
            "diff_artifact": str(diff_path),
            "files_changed": len(checkpoints),
            "insertions": insertions,
            "deletions": deletions,
        },
        summary=f"Applied {len(checkpoints)} transactional changes.",
    )


def rollback_patch_set(
    workspace: str, task_id: str, *, approved_roots: tuple[Path, ...]
) -> ToolResult:
    try:
        root = resolve_approved_path(workspace, approved_roots)
        metadata = root / ".cato" / "artifacts" / task_id / "transaction.json"
        records = json.loads(metadata.read_text())
    except (OSError, ValueError, json.JSONDecodeError, PathOutsideApprovedRoots):
        return ToolResult.failure(
            "Transaction checkpoint was not found.", code="checkpoint_missing"
        )
    for record in records:
        target = Path(record["path"])
        if (
            _hash(target.read_text() if target.exists() else None)
            != record["after_sha256"]
        ):
            return ToolResult.failure(
                "A file changed after Cato's patch.", code="rollback_conflict"
            )
    for record in reversed(records):
        target = Path(record["path"])
        if record["created"]:
            target.unlink(missing_ok=True)
        else:
            shutil.copyfile(record["backup"], target)
            os.chmod(target, record["mode"])
    return ToolResult.success(
        {"rolled_back": len(records)}, summary="Rolled back only Cato-owned changes."
    )


def _rollback(checkpoints: list[SourceCheckpoint]) -> None:
    for item in reversed(checkpoints):
        target = Path(item.path)
        if item.created:
            target.unlink(missing_ok=True)
        elif item.backup:
            shutil.copyfile(item.backup, target)
            os.chmod(target, item.mode)


def _hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value is not None else None


def _validate_source(path: Path, content: str) -> str | None:
    try:
        if path.suffix == ".py":
            ast.parse(content)
        elif path.suffix == ".json":
            json.loads(content)
        elif path.suffix == ".toml":
            tomllib.loads(content)
    except (SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return f"Proposed {path.suffix} content is invalid."
    return None
