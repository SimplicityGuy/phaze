"""Regression coverage for the justfile's no-argument entry point."""

from pathlib import Path
import re
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE_PATH = REPO_ROOT / "justfile"
RECIPE_SIGNATURE = re.compile(r"^([a-z][a-z0-9-]*)(?:\s+[^:=\n]+)?\s*:(?!=)", re.MULTILINE)


def test_default_is_the_first_justfile_recipe() -> None:
    """Keep bare ``just`` from selecting a setup or service recipe by position."""
    recipe_names = RECIPE_SIGNATURE.findall(JUSTFILE_PATH.read_text(encoding="utf-8"))

    assert recipe_names
    assert recipe_names[0] == "default"


def test_bare_just_lists_recipes_without_running_install() -> None:
    """Dry-run the real command-selection path without executing a recipe body."""
    just = shutil.which("just")
    assert just is not None
    result = subprocess.run(  # noqa: S603 - fixed executable and arguments exercise the repository's justfile
        [just, "--dry-run"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "just --list" in output
    assert "uv sync" not in output
