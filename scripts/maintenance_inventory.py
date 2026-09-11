"""Build the reproducible repository-maintenance inventory (phaze-sfofk.1)."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path, PurePosixPath
import subprocess  # nosec B404 -- fixed git argv, no shell, repository-local read operations only
from typing import TYPE_CHECKING, TypedDict


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]

CODE_SURFACE_NAMES = (
    "python",
    "shell",
    "javascript",
    "stylesheet",
    "runtime-template",
    "dockerfile",
    "github-automation",
    "justfile",
)
DOCUMENTATION_CLASS_NAMES = (
    "maintained",
    "accepted-decision",
    "historical-evidence",
    "runtime-loaded",
    "generated",
    "tool-local",
)
EXCLUSION_CLASS_NAMES = (
    "captured-or-binary-fixture",
    "declarative-config-or-data",
    "generated-runtime-asset",
    "repository-marker",
)

_PROSE_SUFFIXES = frozenset({".adoc", ".md", ".mdx", ".rst", ".txt"})
_CONFIG_SUFFIXES = frozenset({".agent", ".example", ".ini", ".json", ".lock", ".toml", ".yaml", ".yml"})
_FIXTURE_SUFFIXES = frozenset({".html", ".ico", ".json", ".png", ".svg", ".wav"})
_GENERATED_MARKER = "^<!-- generated-by:"


class Inventory(TypedDict):
    revision: str
    tracked_files: int
    code_surfaces: dict[str, list[str]]
    documentation: dict[str, list[str]]
    excluded: dict[str, list[str]]
    unclassified: list[str]


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603 B607 -- fixed git executable, no shell
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def resolve_revision(revision: str) -> str:
    """Resolve ``revision`` to the full commit id used by the inventory."""
    return _git("rev-parse", f"{revision}^{{commit}}").stdout.strip()


def tracked_paths(revision: str) -> tuple[str, ...]:
    """Return every tracked path at ``revision`` in byte-stable sorted order."""
    output = _git("ls-tree", "-r", "--name-only", revision).stdout
    return tuple(sorted(line for line in output.splitlines() if line))


def generated_documents(revision: str) -> frozenset[str]:
    """Return Markdown documents carrying an explicit generator ownership marker."""
    completed = _git("grep", "-l", _GENERATED_MARKER, revision, "--", "*.md", check=False)
    if completed.returncode > 1:
        raise RuntimeError(f"git grep failed (rc={completed.returncode}): {completed.stderr.strip()}")
    prefix = f"{revision}:"
    return frozenset(line.removeprefix(prefix) for line in completed.stdout.splitlines() if line)


def code_surface(path: str) -> str | None:
    """Classify a tracked code-comment surface, or return ``None``."""
    pure = PurePosixPath(path)
    name = pure.name
    suffix = pure.suffix
    if suffix == ".py":
        return "python"
    if suffix == ".sh":
        return "shell"
    if suffix == ".js":
        return "javascript"
    if path == "assets/src/app.css":
        return "stylesheet"
    if (path.startswith("src/phaze/templates/") and suffix == ".html") or path == "alembic/script.py.mako":
        return "runtime-template"
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return "dockerfile"
    if (path.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}) or (
        path.startswith(".github/actions/") and name in {"action.yml", "action.yaml"}
    ):
        return "github-automation"
    if path == "justfile":
        return "justfile"
    return None


def is_documentation_surface(path: str) -> bool:
    """Return whether ``path`` belongs to the human-facing documentation corpus."""
    pure = PurePosixPath(path)
    return (
        path.startswith((".planning/", "design/", "docs/"))
        or pure.suffix in _PROSE_SUFFIXES
        or path in {"LICENSE", "alembic/README"}
        or path.startswith(".env.example")
    )


def documentation_class(path: str, *, generated: frozenset[str]) -> str | None:
    """Classify one documentation surface according to the maintenance contract."""
    if not is_documentation_surface(path):
        return None
    if path == "src/phaze/prompts/naming.md":
        return "runtime-loaded"
    if path.startswith("docs/design/") and PurePosixPath(path).suffix == ".md":
        return "accepted-decision"
    if path == "CLAUDE.md":
        return "tool-local"
    if path in generated:
        return "generated"
    if path in {".planning/README.md", "docs/superpowers/specs/README.md"}:
        return "maintained"
    if (
        path.startswith((".planning/", "docs/spikes/", "docs/superpowers/specs/", "docs/telemetry/measurements/"))
        or path == "docs/documentation-audit-2026-08-19.md"
    ):
        return "historical-evidence"
    return "maintained"


def exclusion_class(path: str) -> str | None:
    """Classify tracked files outside both audit populations."""
    pure = PurePosixPath(path)
    suffix = pure.suffix
    if (
        path.startswith(("tests/identify/fixtures/", "tests/vendor/"))
        or path.endswith("_golden.json")
        or path == "tests/analyze/fixtures/real_analyze_file_result.json"
    ):
        return "captured-or-binary-fixture"
    if path.startswith("src/phaze/static/") or (path.startswith("src/phaze/templates/") and suffix == ".svg"):
        return "generated-runtime-asset"
    if pure.name == ".gitkeep" or suffix == ".typed":
        return "repository-marker"
    if suffix in _CONFIG_SUFFIXES or pure.name in {".dockerignore", ".gitignore", ".pip-audit-ignores"}:
        return "declarative-config-or-data"
    if suffix in _FIXTURE_SUFFIXES:
        return "captured-or-binary-fixture"
    return None


def _group(paths: Iterable[str], classifier: Callable[[str], str | None]) -> dict[str, list[str]]:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for path in paths:
        category = classifier(path)
        if category is not None:
            grouped[category].append(path)
    return {name: grouped.get(name, []) for name in sorted(grouped)}


def build_inventory(revision: str = "HEAD") -> Inventory:
    """Build a complete inventory for a committed tree."""
    resolved = resolve_revision(revision)
    paths = tracked_paths(resolved)
    generated = generated_documents(resolved)
    code = _group(paths, code_surface)
    documentation = _group(paths, lambda path: documentation_class(path, generated=generated))
    covered = {path for members in (*code.values(), *documentation.values()) for path in members}
    excluded = _group((path for path in paths if path not in covered), exclusion_class)
    classified = covered | {path for members in excluded.values() for path in members}
    unclassified = sorted(set(paths) - classified)
    return {
        "revision": resolved,
        "tracked_files": len(paths),
        "code_surfaces": code,
        "documentation": documentation,
        "excluded": excluded,
        "unclassified": unclassified,
    }


def summary(inventory: Inventory) -> dict[str, object]:
    """Replace path lists with counts while preserving the inventory shape."""
    return {
        "revision": inventory["revision"],
        "tracked_files": inventory["tracked_files"],
        "code_surfaces": {name: len(paths) for name, paths in inventory["code_surfaces"].items()},
        "documentation": {name: len(paths) for name, paths in inventory["documentation"].items()},
        "excluded": {name: len(paths) for name, paths in inventory["excluded"].items()},
        "unclassified": inventory["unclassified"],
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", default="HEAD", help="committed revision to inventory (default: HEAD)")
    parser.add_argument("--paths", action="store_true", help="emit every path instead of count summaries")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    inventory = build_inventory(args.rev)
    print(json.dumps(inventory if args.paths else summary(inventory), indent=2, sort_keys=True))  # noqa: T201
    return 1 if inventory["unclassified"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
