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


# phaze-aje5d: `just --list` stays one readable line per recipe, the groups say what a recipe
# operates on, and a renamed recipe never strands its old name.
_DOC_RE = re.compile(r"^\[doc\('(?P<doc>[^']*)'\)\]$", re.MULTILINE)
_GROUP_RE = re.compile(r"^\[group\('(?P<group>[^']*)'\)\]$", re.MULTILINE)
_ALIAS_RE = re.compile(r"^alias (?P<old>[a-z][a-z0-9-]*) := (?P<new>[a-z][a-z0-9-]*)$", re.MULTILINE)
# The longest [doc()] `just --list` can print beside the widest recipe signature and an alias tag
# without wrapping an ordinary terminal. Detail belongs in the comment block above the recipe.
_MAX_DOC_CHARS = 60
# The grouping rule the justfile header states, one entry per group it names.
_GROUPS = {"setup", "stack", "docker", "gate", "lint", "test", "ci", "harness", "benchmark", "db", "repowise", "security", "models", "maintenance"}
_RENAMED = {
    "agent-down": "down-agent",
    "cloud-agent-up": "up-cloud-agent",
    "cloud-agent-down": "down-cloud-agent",
    "benchmark-db-up": "up-benchmark-db",
    "benchmark-db-down": "down-benchmark-db",
}


def test_every_doc_string_is_one_concise_line() -> None:
    docs = _DOC_RE.findall(JUSTFILE_PATH.read_text(encoding="utf-8"))
    assert docs, "no [doc()] attributes found -- the pattern no longer matches the justfile"
    too_long = [doc for doc in docs if len(doc) > _MAX_DOC_CHARS]
    assert not too_long, f"[doc()] strings over {_MAX_DOC_CHARS} chars; move the detail into a comment above the recipe: {too_long}"


def test_every_group_is_one_the_header_rule_names() -> None:
    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    used = set(_GROUP_RE.findall(justfile))
    assert used == _GROUPS, f"groups used {sorted(used)} != groups the header names {sorted(_GROUPS)}"
    header = justfile.split("set positional-arguments", 1)[0]
    for group in _GROUPS:
        assert re.search(rf"(?m)^#\s+.*\b{group}\b", header), f"the justfile header's GROUPS rule does not name {group!r}"


def test_every_renamed_recipe_keeps_its_old_name_as_an_alias() -> None:
    aliases = dict(_ALIAS_RE.findall(JUSTFILE_PATH.read_text(encoding="utf-8")))
    public = _public_recipes()
    assert aliases == _RENAMED
    assert set(_RENAMED.values()) <= public
    assert not set(_RENAMED) & public, "an old name is still a recipe rather than an alias"
    just = shutil.which("just") or "just"
    for old, new in _RENAMED.items():
        old_plan = subprocess.run(  # noqa: S603 - fixed executable; recipe names are literals above
            [just, "--dry-run", old], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        )
        new_plan = subprocess.run(  # noqa: S603 - fixed executable; recipe names are literals above
            [just, "--dry-run", new], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        )
        assert old_plan.stderr == new_plan.stderr, f"alias {old} does not run {new}"


def test_the_bh_validation_recipes_were_not_renamed() -> None:
    # The hive's validation boundaries (fleet.yaml: validate_cmd / merge / merge-main / postland /
    # union / molecule) and worktree provisioning name these; renaming one fails a gate silently.
    assert {"check", "check-fast", "check-all", "setup"} <= _public_recipes()
