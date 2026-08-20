#!/usr/bin/env python3
"""Re-derive Repowise structural findings (and per-function CCN/nesting) from SOURCE.

Companion to ``scripts/health_baseline.py``, which reads the Repowise sqlite index. That index
is a gitignored, per-checkout artifact pinned to whatever commit it was built at, so it can
report the BEFORE side of a refactor but never the AFTER side without a full re-index. This
script closes that gap: it drives Repowise's own complexity walker over the working tree and
re-applies the four structural biomarker rules, so "findings cleared" and "max CCN / max nesting
after" become measured facts on any commit, in any worktree, with no index.

Written for epic phaze-vu88k (bead .6). Bead .9 re-measures the same population on the landed
commit and needs exactly this.

WHAT IT MEASURES
    complex_method      ccn >= 9
    nested_complexity   max_nesting >= 4
    large_method        nloc >= 60 AND ccn >= 3
    bumpy_road          bumps >= 3 AND ccn >= 5

THRESHOLD PROVENANCE -- these were READ from the installed plugin's biomarker sources, not
assumed. If the plugin's thresholds move, this script goes stale silently, so the next reader
needs to know where they came from (paths relative to PLUGIN_ROOT below):

    packages/core/src/repowise/core/analysis/health/biomarkers/complex_method.py:17
        _CCN_THRESHOLD = 9
    packages/core/src/repowise/core/analysis/health/biomarkers/nested_complexity.py:17
        _NESTING_THRESHOLD = 4
    packages/core/src/repowise/core/analysis/health/biomarkers/large_method.py:23,34
        _NLOC_THRESHOLD = 60, _CCN_FLOOR = 3
    packages/core/src/repowise/core/analysis/health/biomarkers/bumpy_road.py:23,24
        _BUMP_THRESHOLD = 3, _CCN_THRESHOLD = 5

``large_method``'s CCN floor is deliberate and worth knowing before you "fix" a finding: the
detector excludes a long-but-FLAT body as a layout artefact rather than a maintainability smell,
and the floor is 3 rather than 2 specifically so a flat match/case dispatch table is skipped. A
long function at CCN 3-6 is therefore only barely a finding, and splitting it to get under a line
count is refactoring for the metric.

MACHINE-LOCAL DEPENDENCY
    This imports the walker from the installed `repowise` Claude Code plugin, under
    ``~/.claude/plugins/marketplaces/repowise/``. That is NOT a project dependency and is NOT in
    pyproject.toml -- on a machine without the plugin this script exits 2 with an explanatory
    message rather than a traceback. It also needs the plugin's own runtime deps (structlog,
    tree-sitter), so run it with the plugin's interpreter:

        ~/.local/share/uv/tools/repowise/bin/python scripts/structural_findings.py <paths>

VALIDATION
    Against the frozen phaze-vu88k.1 baseline at commit ef1ef7d2, over the 12 below-6.0 routers:
    max_ccn, max_nesting and nloc reproduced 12/12 files exactly, and the re-derived findings
    reproduced 32/32 -- same functions, same biomarkers. It is not an approximation of the
    detectors; it drives them.

    Independently cross-validated: bead phaze-vu88k.5's developer built an equivalent instrument
    and ran it over three of this bead's files (execution.py, pipeline_scans.py, shell.py),
    agreeing 10/10 on biomarker, severity, function, line and every detail value -- including
    ``start_execution`` at ccn 19 / cognitive 44 / nloc 165. Two independent implementations both
    reproducing the ledger is stronger evidence than either alone.

FREE-VARIABLE MODE
    ``--freevars`` reports names a function loads that resolve to NOTHING in any enclosing scope.
    Extraction refactors lift a block across a scope boundary, so a name the block relied on from
    its old enclosing function silently becomes unbound -- typically on an ERROR path that neither
    the happy-path tests nor a randomized differential harness ever reaches. This bug class was
    shipped once during epic phaze-vu88k and `ruff` F821 did NOT catch it.

    Note the asymmetry, because this mode only covers half the risk: it catches a free variable
    that resolves to nothing. It CANNOT catch one that resolves to the WRONG EXISTING name -- a
    careless rename (``agent.id`` -> ``agent_id`` also rewriting ``fileserver_agent.id``) still
    resolves, so only reading the diff or a test catches that.

Usage:
    scripts/structural_findings.py <path>...              # findings + per-file metrics (JSON)
    scripts/structural_findings.py --summary <path>...    # one line per file
    scripts/structural_findings.py --freevars <path>...   # unresolved free variables; exit 1 if any
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
from pathlib import Path
import sys


PLUGIN_ROOT = Path.home() / ".claude" / "plugins" / "marketplaces" / "repowise"
WALKER_SRC = PLUGIN_ROOT / "packages" / "core" / "src"

# (biomarker, predicate) -- see THRESHOLD PROVENANCE above.
BIOMARKERS = (
    ("complex_method", lambda f: f.ccn >= 9),
    ("nested_complexity", lambda f: f.max_nesting >= 4),
    ("large_method", lambda f: f.nloc >= 60 and f.ccn >= 3),
    ("bumpy_road", lambda f: f.bumps >= 3 and f.ccn >= 5),
)

_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_MODULE_DUNDERS = frozenset({"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__", "__builtins__", "__debug__"})


def _load_walker():  # type: ignore[no-untyped-def]
    """Import Repowise's own complexity walker, or exit 2 with an actionable message."""
    if not WALKER_SRC.is_dir():
        sys.exit(
            f"repowise plugin not found at {PLUGIN_ROOT}\n"
            "This script drives the plugin's own complexity walker and cannot run without it.\n"
            "Install the repowise Claude Code plugin, or run this on a machine that has it."
        )
    sys.path.insert(0, str(WALKER_SRC))
    try:
        # PLC0415 is suppressed below deliberately: a module-level import would raise
        # ImportError before the friendly plugin-missing message above could ever run.
        from repowise.core.analysis.health.complexity.walker import walk_file  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError as exc:
        sys.exit(
            f"could not import the repowise walker: {exc}\n"
            "It needs the plugin's own runtime deps (structlog, tree-sitter). Run this with the\n"
            "plugin's interpreter, e.g. ~/.local/share/uv/tools/repowise/bin/python"
        )
    return walk_file


def measure(path: str) -> dict:
    """Per-function structural findings plus the file's max CCN / max nesting."""
    walk_file = _load_walker()
    functions = walk_file(path, "python", Path(path).read_bytes()).functions
    flagged = []
    for fn in functions:
        hits = [name for name, predicate in BIOMARKERS if predicate(fn)]
        if hits:
            flagged.append(
                {
                    "function": fn.name,
                    "line_start": fn.start_line,
                    "line_end": fn.end_line,
                    "ccn": fn.ccn,
                    "max_nesting": fn.max_nesting,
                    "nloc": fn.nloc,
                    "bumps": fn.bumps,
                    "findings": hits,
                }
            )
    return {
        "path": path,
        "max_ccn": max((f.ccn for f in functions), default=0),
        "max_nesting": max((f.max_nesting for f in functions), default=0),
        "n_findings": sum(len(f["findings"]) for f in flagged),
        "functions": sorted(flagged, key=lambda d: (-len(d["findings"]), d["line_start"])),
    }


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node inside ``scope`` that is not itself inside a NESTED scope.

    A comprehension's first iterable is evaluated in the ENCLOSING scope, so it stays visible
    here even though the comprehension is its own scope.
    """
    stack: list[ast.AST] = [scope]
    seen: list[ast.AST] = []
    while stack:
        for child in ast.iter_child_nodes(stack.pop()):
            seen.append(child)
            if not isinstance(child, _SCOPES):
                stack.append(child)
            elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) and child.generators:
                stack.append(child.generators[0].iter)
    return seen


def _own_bindings(scope: ast.AST) -> set[str]:
    """Names bound by THIS scope, without descending into nested scopes."""
    out: set[str] = set()
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            out.add(arg.arg)
        if args.vararg:
            out.add(args.vararg.arg)
        if args.kwarg:
            out.add(args.kwarg.arg)
    for node in _own_nodes(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
        elif isinstance(node, ast.comprehension):
            out.update(t.id for t in ast.walk(node.target) if isinstance(t, ast.Name))
    return out


def free_variables(path: str) -> list[tuple[str, int, list[str]]]:
    """Scopes in ``path`` that load a name resolving to nothing anywhere up the scope chain."""
    tree = ast.parse(Path(path).read_text())
    problems: list[tuple[str, int, list[str]]] = []

    def walk(scope: ast.AST, visible: set[str], label: str) -> None:
        here = visible | _own_bindings(scope)
        loads = {n.id for n in _own_nodes(scope) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(loads - here)
        if unresolved:
            problems.append((label.lstrip("."), getattr(scope, "lineno", 0), unresolved))
        for child in _own_nodes(scope):
            if isinstance(child, (*_SCOPES, ast.ClassDef)):
                walk(child, here, f"{label}.{getattr(child, 'name', type(child).__name__)}")

    walk(tree, set(dir(builtins)) | _MODULE_DUNDERS, "")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="Python files to measure")
    parser.add_argument("--summary", action="store_true", help="one line per file instead of JSON")
    parser.add_argument("--freevars", action="store_true", help="report unresolved free variables; exit 1 if any")
    args = parser.parse_args()

    out = sys.stdout

    if args.freevars:
        total = 0
        for path in args.paths:
            for label, line, names in free_variables(path):
                total += 1
                out.write(f"{path}:{line}  {label}  UNRESOLVED: {', '.join(names)}\n")
        out.write(f"unresolved free variables: {total}\n")
        return 1 if total else 0

    results = [measure(p) for p in args.paths]
    if args.summary:
        for r in results:
            out.write(f"{r['path']:50s} max_ccn={r['max_ccn']:3d} max_nesting={r['max_nesting']:2d} findings={r['n_findings']}\n")
        out.write(f"{'TOTAL':50s} findings={sum(r['n_findings'] for r in results)}\n")
    else:
        out.write(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
