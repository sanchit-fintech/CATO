from __future__ import annotations

import hashlib
from pathlib import Path

from tools.transaction import apply_patch_set, rollback_patch_set


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_two_file_transaction_and_rollback(tmp_path: Path) -> None:
    first, second = tmp_path / "first.py", tmp_path / "second.json"
    first.write_text("value = 1\n")
    second.write_text('{"value": 1}\n')
    result = apply_patch_set(
        str(tmp_path),
        "task-one",
        [
            {
                "kind": "replace",
                "path": "first.py",
                "old_text": "1",
                "new_text": "2",
                "expected_sha256": digest(first),
            },
            {
                "kind": "replace",
                "path": "second.json",
                "old_text": "1",
                "new_text": "2",
                "expected_sha256": digest(second),
            },
        ],
        approved_roots=(tmp_path,),
    )
    assert result.ok
    assert result.data["files_changed"] == 2
    assert Path(result.data["diff_artifact"]).read_text().startswith("--- a/first.py")
    assert "2" in first.read_text() and "2" in second.read_text()
    rolled_back = rollback_patch_set(
        str(tmp_path), "task-one", approved_roots=(tmp_path,)
    )
    assert rolled_back.ok
    assert first.read_text() == "value = 1\n"
    assert second.read_text() == '{"value": 1}\n'


def test_stale_second_file_makes_transaction_atomic(tmp_path: Path) -> None:
    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_text("a = 1\n")
    second.write_text("b = 1\n")
    result = apply_patch_set(
        str(tmp_path),
        "task-two",
        [
            {
                "kind": "replace",
                "path": "first.py",
                "old_text": "1",
                "new_text": "2",
                "expected_sha256": digest(first),
            },
            {
                "kind": "replace",
                "path": "second.py",
                "old_text": "1",
                "new_text": "2",
                "expected_sha256": "0" * 64,
            },
        ],
        approved_roots=(tmp_path,),
    )
    assert not result.ok and result.code == "hash_mismatch"
    assert first.read_text() == "a = 1\n"
    assert second.read_text() == "b = 1\n"


def test_rollback_refuses_user_edit_after_transaction(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n")
    result = apply_patch_set(
        str(tmp_path),
        "task-three",
        [
            {
                "kind": "replace",
                "path": "app.py",
                "old_text": "1",
                "new_text": "2",
                "expected_sha256": digest(target),
            }
        ],
        approved_roots=(tmp_path,),
    )
    assert result.ok
    target.write_text("value = 3\n")
    rollback = rollback_patch_set(
        str(tmp_path), "task-three", approved_roots=(tmp_path,)
    )
    assert not rollback.ok and rollback.code == "rollback_conflict"
    assert target.read_text() == "value = 3\n"
