"""`scripts/worktree-integrity.sh` -- the loud checkout-integrity probe in front of both gates (phaze-ofrxb).

The script exists because a hollowed checkout used to surface as ``ModuleNotFoundError`` inside
ruff/mypy/pytest, indistinguishable from a code regression. These tests exercise the script the
way the gate does -- as a subprocess on a real (temporary) git repository -- and pin the four
findings it must name, the remedy text a developer acts on, and the justfile wiring that puts it
FIRST in both ``check`` and ``check-fast`` (before anything that needs the venv).

No test here touches the repo's own checkout or venv: the healthy case symlinks the interpreter
running these tests into a fake ``.venv`` so the module probe resolves against modules that are
known to exist, and the hollowed case installs a fake ``python`` that reports the exact failure
shape the purge produced (an import that fails).
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "worktree-integrity.sh"
_JUSTFILE = (_ROOT / "justfile").read_text(encoding="utf-8")

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    # A global core.fsmonitor=true would spawn a daemon per throwaway repo; keep the tests inert.
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.fsmonitor",
    "GIT_CONFIG_VALUE_0": "false",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=_GIT_ENV, capture_output=True)  # noqa: S603, S607


def _make_checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "seat"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text('[project]\nname = "seat"\n', encoding="utf-8")
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(_SCRIPT), str(repo)], capture_output=True, text=True, env=_GIT_ENV, check=False)  # noqa: S603, S607


def _healthy_venv(repo: Path) -> None:
    """A fake `.venv/bin/python` that IS the interpreter running these tests.

    Not a symlink: a venv interpreter locates its `pyvenv.cfg` relative to the path it was
    invoked through, so a symlink from an empty `.venv` would run without site-packages and
    look hollowed. An exec shim keeps the real venv (and its mypy/pytest/defusedxml) in play.
    """
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    shim = venv_bin / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)


def _hollowed_venv(repo: Path) -> None:
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake = venv_bin / "python"
    fake.write_text("#!/bin/sh\necho \"ModuleNotFoundError: No module named 'defusedxml'\" >&2\nexit 1\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)


def test_a_healthy_checkout_passes_silently(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path)
    _healthy_venv(repo)

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_a_checkout_without_a_venv_is_still_healthy(tmp_path: Path) -> None:
    """`uv run` creates the venv on first use; its absence is not the purge."""
    result = _run(_make_checkout(tmp_path))

    assert result.returncode == 0, result.stderr


def test_a_missing_git_pointer_is_named_with_the_resume_remedy(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path)
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "wt/bead/issue/x")
    seat = tmp_path / "wt"
    assert (seat / ".git").is_file(), "a linked worktree carries a .git POINTER FILE, which is what the purge removed"
    (seat / ".git").unlink()

    result = _run(seat)

    assert result.returncode == 1
    assert ".git is missing" in result.stderr
    assert "bh work resume" in result.stderr
    assert "FAILED" in result.stderr


def test_a_missing_pyproject_is_named(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path)
    (repo / "pyproject.toml").unlink()

    result = _run(repo)

    assert result.returncode == 1
    assert "pyproject.toml is missing" in result.stderr
    # The same run also reports it as a deleted tracked file -- one cause, both symptoms named.
    assert "1 tracked file(s) missing from disk" in result.stderr
    assert "pyproject.toml" in result.stderr


def test_deleted_tracked_files_are_counted_and_listed(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path)
    (repo / "tracked.py").unlink()

    result = _run(repo)

    assert result.returncode == 1
    assert "1 tracked file(s) missing from disk" in result.stderr
    assert "tracked.py" in result.stderr
    assert "git checkout -- ." in result.stderr


def test_a_hollowed_venv_is_named_as_purge_not_regression(tmp_path: Path) -> None:
    repo = _make_checkout(tmp_path)
    _hollowed_venv(repo)

    result = _run(repo)

    assert result.returncode == 1
    assert "hollowed" in result.stderr or "could not run the import probe" in result.stderr
    assert "not a code regression" in result.stderr or "rebuild the venv" in result.stderr
    assert "rm -rf .venv && uv sync" in result.stderr


def test_the_probe_reports_every_missing_module_not_just_the_first(tmp_path: Path) -> None:
    """A real interpreter with two of the three gate modules absent must name both."""
    repo = _make_checkout(tmp_path)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake = venv_bin / "python"
    # Run the real interpreter in isolated mode with an empty site so mypy/pytest/defusedxml are all absent.
    fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -S "$@"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    result = _run(repo)

    assert result.returncode == 1
    for name in ("mypy", "pytest", "defusedxml"):
        assert f"missing {name}:" in result.stderr


@pytest.mark.parametrize("recipe", ["check", "check-fast"])
def test_both_gates_run_the_integrity_probe_before_anything_that_needs_the_venv(recipe: str) -> None:
    match = re.search(rf"^{re.escape(recipe)}: (.+)$", _JUSTFILE, re.MULTILINE)
    assert match is not None, f"recipe {recipe!r} is missing"
    deps = match.group(1).split()
    assert deps[0] == "worktree-integrity", f"{recipe} must probe the checkout FIRST, got {deps}"
    assert "lint" in deps and "typecheck" in deps


def test_the_probe_recipe_runs_the_script_outside_uv() -> None:
    match = re.search(r"^worktree-integrity:\n((?: +.*\n)+)", _JUSTFILE, re.MULTILINE)
    assert match is not None
    body = match.group(1).strip()
    assert body == "bash scripts/worktree-integrity.sh"
    assert "uv run" not in body, "the probe must not depend on the venv it is checking"
