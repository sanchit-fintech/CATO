from __future__ import annotations

from pathlib import Path

from tools.developer import run_project_tests


def test_project_test_runner_parses_and_bounds_results(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    result = run_project_tests(
        str(tmp_path), approved_roots=(tmp_path,), timeout=20, max_output_bytes=5_000
    )
    assert result.ok
    assert result.data["return_code"] == 0
    assert result.data["results"]["passed"] == 1


def test_project_test_runner_rejects_traversal_target(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    result = run_project_tests(
        str(tmp_path), approved_roots=(tmp_path,), target="../outside.py"
    )
    assert not result.ok
    assert result.code == "path_not_approved"
