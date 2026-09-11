"""Integrity guards for the maintained developer-documentation entrypoints."""

from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote

from scripts import maintenance_inventory


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCUMENTATION_INDEX = REPO_ROOT / "docs" / "README.md"
CURRENT_ENTRYPOINTS = (
    REPO_ROOT / "README.md",
    DOCUMENTATION_INDEX,
    REPO_ROOT / "docs" / "quick-start.md",
    REPO_ROOT / "docs" / "AGF.md",
)
LINK_RE = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")
INLINE_JUST_RECIPE_RE = re.compile(r"`just ([a-z][a-z0-9-]*)")
COMMAND_JUST_RECIPE_RE = re.compile(r"^\s*just ([a-z][a-z0-9-]*)", re.MULTILINE)


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 -- git is the repository's required source-of-truth tool
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def _generated_documents(paths: set[str]) -> frozenset[str]:
    return frozenset(path for path in paths if path.endswith(".md") and "<!-- generated-by:" in (REPO_ROOT / path).read_text(encoding="utf-8"))


def _local_targets(document: Path) -> set[Path]:
    targets: set[Path] = set()
    for match in LINK_RE.finditer(document.read_text(encoding="utf-8")):
        raw_target = match.group(1).strip().strip("<>").split(maxsplit=1)[0]
        if raw_target.startswith(("#", "https://", "http://", "mailto:")):
            continue
        path_part = unquote(raw_target.split("#", maxsplit=1)[0].split("?", maxsplit=1)[0])
        targets.add((document.parent / path_part).resolve())
    return targets


def _public_recipes() -> set[str]:
    just = shutil.which("just")
    assert just is not None, "just is not on PATH"
    result = subprocess.run(  # noqa: S603 -- fixed executable and argument inspect the local justfile
        [just, "--summary"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split())


def test_current_developer_documentation_has_no_broken_relative_links() -> None:
    documents = (
        *CURRENT_ENTRYPOINTS,
        REPO_ROOT / "docs" / "gates-and-isolation.md",
        REPO_ROOT / "docs" / "git-topology-and-verification.md",
        REPO_ROOT / "docs" / "just-recipe-contract.md",
        REPO_ROOT / "docs" / "repository-maintenance-contract.md",
        REPO_ROOT / "src" / "phaze" / "agent_watcher" / "README.md",
        REPO_ROOT / "tests" / "BUCKETS.md",
    )

    missing = {
        f"{document.relative_to(REPO_ROOT)} -> {target.relative_to(REPO_ROOT)}"
        for document in documents
        for target in _local_targets(document)
        if not target.exists()
    }

    assert not missing, "broken maintained-document links:\n" + "\n".join(sorted(missing))


def test_documentation_index_lists_every_maintained_markdown_guide() -> None:
    tracked = _tracked_paths()
    generated = _generated_documents(tracked)
    maintained_guides = {
        (REPO_ROOT / path).resolve()
        for path in tracked
        if path.endswith(".md") and maintenance_inventory.documentation_class(path, generated=generated) == "maintained"
    }
    maintained_guides.remove(DOCUMENTATION_INDEX.resolve())

    missing = maintained_guides - _local_targets(DOCUMENTATION_INDEX)

    assert not missing, "maintained guides missing from docs/README.md:\n" + "\n".join(sorted(str(path.relative_to(REPO_ROOT)) for path in missing))


def test_current_entrypoints_use_public_just_recipes() -> None:
    public = _public_recipes()
    cited = {
        recipe
        for document in CURRENT_ENTRYPOINTS
        for recipe in (
            INLINE_JUST_RECIPE_RE.findall(document.read_text(encoding="utf-8")) + COMMAND_JUST_RECIPE_RE.findall(document.read_text(encoding="utf-8"))
        )
    }

    assert cited <= public, f"developer entrypoints cite non-public Just recipes: {sorted(cited - public)}"


def test_current_entrypoints_own_their_prose_and_state_the_isolation_contract() -> None:
    for document in CURRENT_ENTRYPOINTS:
        assert "<!-- generated-by:" not in document.read_text(encoding="utf-8"), document.relative_to(REPO_ROOT)

    agf = (REPO_ROOT / "docs" / "AGF.md").read_text(encoding="utf-8")
    assert "just test-db-for <seat>" in agf
    assert "just test-db-release <seat>" in agf
    assert "never use `just test-db-down`" in agf
    assert "copy all three exports" in agf
    assert "Do not run two pytest processes against one seat" in agf
