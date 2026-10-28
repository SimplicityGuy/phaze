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
    r"(external contract|operator convenience|internal helper|historical one-off) \| (.+?) \| (?:—|`[a-z][a-z0-9-]*`(?:, `[a-z][a-z0-9-]*`)*) \|$",
    re.MULTILINE,
)
# The inventory's last column: the pre-rename name(s) each recipe still answers to as an alias.
_ROW_ALIASES_RE = re.compile(r"^\| `([a-z][a-z0-9-]*)` \| [^|]+ \| [^|]+ \| ([^|]+) \|$", re.MULTILINE)


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
    # Operator decisions 9 and 10c on phaze-aje5d (2026-10-28): removed outright, under either name,
    # with no alias left behind.
    removed = {
        "docker-shell",
        "shell-api",
        "fmt",
        "format",
        "lint-fix",
        "fix-lint",
        "security-all",
        "check-security",
        "vulture",
        "run-vulture",
        "worker-restart",
        "restart-worker",
        "image-push-arm64",
        "push-image-arm64",
        "integration-test-down",
        "down-integration-test",
        "test-file",
    }
    aliases = set(_ALIAS_RE.findall(JUSTFILE_PATH.read_text(encoding="utf-8")))
    assert not removed & recipes
    assert not removed & {name for pair in aliases for name in pair}


def test_required_external_recipe_names_remain_public() -> None:
    assert {"default", "setup", "check-fast", "check-all"} <= _public_recipes()


def test_setup_is_the_single_dependency_sync_primitive() -> None:
    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    uv_sync_lines = [line.strip() for line in justfile.splitlines() if line.strip() == "uv sync"]

    assert uv_sync_lines == ["uv sync"]
    assert "install: setup build-tailwind" in justfile
    assert "just upgrade-lock && just setup" in (REPO_ROOT / "scripts" / "update-project.sh").read_text(encoding="utf-8")


# phaze-aje5d: `just --list` stays one readable line per recipe, the groups say what a recipe
# operates on, and a renamed recipe never strands its old name. _RENAMED is the full old -> new
# table (operator decision 7 on phaze-aje5d, 2026-10-27: "Full verb-noun (~40)"), written out
# here rather than read from the justfile so that deleting an alias there fails this test.
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
    "alerts-test": "test-alerts",
    "bandit": "run-bandit",
    "benchmark-analyze": "measure-analyze-workspace",
    "benchmark-db-down": "down-benchmark-db",
    "benchmark-db-up": "up-benchmark-db",
    "benchmark-explain": "explain-benchmark-db",
    "benchmark-seed": "seed-benchmark-db",
    "branch-check": "check-branch-coverage",
    "cloud-agent-down": "down-cloud-agent",
    "cloud-agent-up": "up-cloud-agent",
    "corpus-distribution": "measure-corpus-distribution",
    "coverage-combine": "combine-coverage",
    "db-current": "show-db-revision",
    "db-downgrade": "downgrade-db",
    "db-history": "show-db-history",
    "db-revision": "create-db-revision",
    "db-upgrade": "upgrade-db",
    "docker-build": "build-docker",
    "docker-compose-validate": "validate-compose",
    "docker-ps": "ps-docker",
    "docker-validate": "validate-dockerfiles",
    "docs-check": "check-docs",
    "image-build-arm64": "build-image-arm64",
    "image-push": "push-image",
    "integration-test": "test-integration",
    "lock-upgrade": "upgrade-lock",
    "parity-check": "check-parity",
    "parity-dump": "dump-parity",
    "parity-golden-regen": "regen-parity-golden",
    "pip-audit": "run-pip-audit",
    "pre-commit": "run-pre-commit",
    "repowise-coverage": "refresh-repowise-coverage",
    "repowise-coverage-ci": "refresh-repowise-coverage-ci",
    "tailwind": "build-tailwind",
    "test-browser-install": "install-test-browser",
    "test-browser-install-ci": "install-test-browser-ci",
    "test-db": "up-test-db",
    "test-db-down": "down-test-db",
    "test-db-for": "provision-test-db-seat",
    "test-db-gc": "gc-test-dbs",
    "test-db-reclaim": "reclaim-test-db-seats",
    "test-db-release": "release-test-db-seat",
    "test-db-seats": "list-test-db-seats",
    "worker-health": "check-worker-health",
    "worker-logs": "logs-worker",
    "worktree-integrity": "check-worktree-integrity",
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

    def show(name: str) -> list[str]:
        # `just --show` resolves an alias exactly as invoking it does, with no arguments needed.
        result = subprocess.run(  # noqa: S603 - fixed executable; recipe names are literals above
            [just, "--show", name], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        )
        return [line for line in result.stdout.splitlines() if not line.startswith("alias ")]

    for old, new in _RENAMED.items():
        assert show(old) == show(new), f"alias {old} does not resolve to recipe {new}"


def test_the_bh_validation_recipes_were_not_renamed() -> None:
    # The hive's validation boundaries (fleet.yaml: validate_cmd / merge / merge-main / postland /
    # union / molecule) and worktree provisioning name these; renaming one fails a gate silently.
    assert {"check", "check-fast", "check-all", "setup"} <= _public_recipes()


def test_the_inventory_names_every_alias_beside_its_recipe() -> None:
    documented: dict[str, str] = {}
    for recipe, cell in _ROW_ALIASES_RE.findall(CONTRACT_PATH.read_text(encoding="utf-8")):
        for old in re.findall(r"`([a-z][a-z0-9-]*)`", cell):
            documented[old] = recipe
    assert documented == _RENAMED


def test_the_bh_validation_recipes_keep_their_exact_names() -> None:
    # Never renamed, so never aliased either: an alias of one of these would mean it moved.
    assert not {"check", "check-fast", "check-all", "setup"} & (set(_RENAMED.values()) | set(_RENAMED))
