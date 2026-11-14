"""phaze-vg1jm: mypy honours .gitignore, so it checks the file set ruff checks.

`mypy .` walks the directory; without `exclude_gitignore = true` a gitignored scratch `.py` with a
type error or a duplicate module name made a gate UNMEASURED while ruff passed. These tests build a
throwaway git repo, run the real mypy on it with and without the setting (the control proves the
fixture would have rejected the old behaviour), and pin the setting in the repo's own pyproject.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GIT_ENV_VARS = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")


def _env() -> dict[str, str]:
    """Strip the git plumbing variables a hook or worktree gate exports, so the temp repo is its own."""
    return {k: v for k, v in os.environ.items() if k not in GIT_ENV_VARS}


def _make_repo(root: Path, *, exclude_gitignore: bool) -> None:
    setting = "exclude_gitignore = true\n" if exclude_gitignore else ""
    (root / "pyproject.toml").write_text(f"[tool.mypy]\n{setting}", encoding="utf-8")
    (root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    (root / "tracked").mkdir()
    (root / "tracked" / "compare.py").write_text("X: int = 1\n", encoding="utf-8")
    (root / "scratch").mkdir()
    # A type error AND a module name that duplicates a tracked one.
    (root / "scratch" / "compare.py").write_text("Y: int = 'not an int'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, env=_env())  # noqa: S607


def _mypy(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "mypy", "."], cwd=root, capture_output=True, text=True, check=False, env=_env())


def test_without_exclude_gitignore_mypy_fails_on_the_gitignored_file(tmp_path: Path) -> None:
    """Control: the old behaviour rejects this tree, so the fixture is discriminating."""
    _make_repo(tmp_path, exclude_gitignore=False)
    result = _mypy(tmp_path)
    assert result.returncode != 0
    assert "scratch" in result.stdout


def test_with_exclude_gitignore_mypy_ignores_the_gitignored_file(tmp_path: Path) -> None:
    _make_repo(tmp_path, exclude_gitignore=True)
    result = _mypy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scratch" not in result.stdout


def test_repo_pyproject_turns_exclude_gitignore_on() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["mypy"].get("exclude_gitignore") is True


@pytest.mark.parametrize("path", ["justfile", ".pre-commit-config.yaml"])
def test_callers_still_run_plain_mypy(path: str) -> None:
    """The scope lives in config, so `uv run mypy .` stays correct for every caller."""
    assert "uv run mypy ." in (REPO_ROOT / path).read_text(encoding="utf-8")
