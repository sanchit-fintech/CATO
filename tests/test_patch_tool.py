from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from tools.patch import apply_text_patch


def test_validated_patch_applies_to_clean_repository(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Cato Test",
            "-c",
            "user.email=cato@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    result = apply_text_patch(
        str(target),
        "value = 1",
        "value = 2",
        expected_sha256=digest,
        approved_roots=(tmp_path,),
    )
    assert result.ok
    assert target.read_text() == "value = 2\n"


def test_patch_refuses_preexisting_dirty_file(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    target.write_text("user change\n")
    result = apply_text_patch(
        str(target), "user change", "cato change", approved_roots=(tmp_path,)
    )
    assert not result.ok
    assert result.code == "dirty_worktree"
    assert target.read_text() == "user change\n"


def test_patch_refuses_hash_or_context_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n")
    result = apply_text_patch(
        str(target),
        "value = 1",
        "value = 2",
        expected_sha256="0" * 64,
        approved_roots=(tmp_path,),
    )
    assert not result.ok
    assert result.code == "hash_mismatch"
    assert target.read_text() == "value = 1\n"
