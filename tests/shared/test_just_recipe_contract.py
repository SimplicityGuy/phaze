"""Executable inventory for the public Just recipe surface."""

from pathlib import Path
import re
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE_PATH = REPO_ROOT / "justfile"
CONTRACT_PATH = REPO_ROOT / "docs" / "just-recipe-contract.md"
_ROW_RE = re.compile(
    r"^\| `([a-z][a-z0-9-]*)` \| "
    r"(external contract|operator convenience|internal helper|historical one-off) \| (.+) \|$",
    re.MULTILINE,
)


def _public_recipes() -> set[str]:
    just = shutil.which("just")
    assert just is not None, "just is not on PATH"
    result = subprocess.run(  # noqa: S603 - fixed executable and arguments inspect this repository's justfile
        [just, "--summary"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split())


def test_every_public_recipe_has_one_documented_class_and_consumer() -> None:
    rows = _ROW_RE.findall(CONTRACT_PATH.read_text(encoding="utf-8"))
    names = [name for name, _classification, _consumer in rows]

    assert len(names) == len(set(names)), "the Just contract inventories a recipe more than once"
    assert set(names) == _public_recipes()
    assert all(consumer.strip() for _name, _classification, consumer in rows)


def test_obsolete_public_recipes_and_unused_descriptions_are_gone() -> None:
    recipes = _public_recipes()
    listing = subprocess.run(  # noqa: S603 - fixed executable and arguments inspect this repository's justfile
        [shutil.which("just") or "just", "--list"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "test-ci" not in recipes
    assert "sync" not in recipes
    assert "UNUSED" not in listing


def test_required_external_recipe_names_remain_public() -> None:
    assert {"default", "setup", "check-fast", "check-all"} <= _public_recipes()


def test_setup_is_the_single_dependency_sync_primitive() -> None:
    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    uv_sync_lines = [line.strip() for line in justfile.splitlines() if line.strip() == "uv sync"]

    assert uv_sync_lines == ["uv sync"]
    assert "install: setup tailwind" in justfile
    assert "just lock-upgrade && just setup" in (REPO_ROOT / "scripts" / "update-project.sh").read_text(encoding="utf-8")
