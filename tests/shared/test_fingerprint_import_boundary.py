"""T-84-04-01: ``services/fingerprint.py`` must not drag the ORM into the agent-worker import graph.

``services/fingerprint.py`` is imported by the **agent worker**, which runs without
``phaze.database`` / ``phaze.models`` available (Phase 26 Plan 10/11, 84-CONTEXT D-00e). Every DB
dependency ``get_fingerprint_progress`` consumes -- ``MUSIC_VIDEO_TYPES``, ``done_clause``,
``failed_clause``, ``dedup_resolved_clause``, ``Stage`` -- is therefore imported **inside** the
function body, not at module scope. Hoisting any one of them to a top-level import crashes the worker
at import time.

Before this guard existed the contract was honoured in source but locked by nothing: the Phase-84 AST
source scan (``test_dedup_fingerprint_source_scan.py``) only inspects scalar-state attribute access,
and 84-04's "sys.modules leak spot-check" was performed once by hand and never committed. A refactor
moving an import to module scope would have shipped green and taken down the worker in production.
That is the "mitigation exists in source but nothing tests it" failure mode.

Two independent checks, because each catches what the other misses:

1. :func:`test_no_forbidden_module_level_imports` -- an AST scan of the *direct* module-level imports.
   Precise, and names the offending line. Blind to a forbidden module pulled in *transitively*.
2. :func:`test_importing_fingerprint_does_not_load_orm` -- imports the module in a **fresh
   interpreter** and asserts the forbidden packages are absent from ``sys.modules``. This is the real
   runtime contract and catches transitive drags that the AST scan cannot see.

Both must go RED if a forbidden import is reintroduced -- mutation-verified before this guard shipped.
DB-free; belongs in the ``shared`` bucket.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys


_FINGERPRINT_SRC = pathlib.Path(__file__).resolve().parents[1] / ".." / "src" / "phaze" / "services" / "fingerprint.py"

# The agent worker has none of these on its import path.
FORBIDDEN_PREFIXES = (
    "phaze.models",
    "phaze.database",
    "phaze.services.pipeline",
    "phaze.services.stage_status",
)


def _is_forbidden(module: str | None) -> bool:
    """True when ``module`` is (or lives under) a package the agent worker cannot import."""
    if not module:
        return False
    return any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def test_no_forbidden_module_level_imports() -> None:
    """No top-level ``import``/``from ... import`` in fingerprint.py touches the ORM or the DB engine."""
    tree = ast.parse(_FINGERPRINT_SRC.resolve().read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in tree.body:  # module scope ONLY -- function-local imports are the required pattern
        if isinstance(node, ast.ImportFrom) and _is_forbidden(node.module):
            offenders.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            offenders.extend(f"line {node.lineno}: import {alias.name}" for alias in node.names if _is_forbidden(alias.name))

    assert not offenders, (
        "services/fingerprint.py has module-level DB imports; the agent worker will crash at import time (D-00e). "
        f"Move these inside get_fingerprint_progress: {offenders}"
    )


def test_importing_fingerprint_does_not_load_orm() -> None:
    """In a fresh interpreter, importing fingerprint.py must not load phaze.models / phaze.database.

    Runs out-of-process so an ORM module already imported by a sibling test cannot mask the leak.
    """
    probe = (
        "import sys, importlib;"
        "importlib.import_module('phaze.services.fingerprint');"
        "prefixes = " + repr(FORBIDDEN_PREFIXES) + ";"
        "leaked = sorted(m for m in sys.modules if any(m == p or m.startswith(p + '.') for p in prefixes));"
        "print(','.join(leaked))"
    )
    # S603: the argv is `sys.executable` plus a literal probe string built above — no untrusted input.
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)  # noqa: S603

    assert result.returncode == 0, f"importing phaze.services.fingerprint failed: {result.stderr}"
    leaked = [m for m in result.stdout.strip().split(",") if m]
    assert not leaked, f"importing services/fingerprint.py loaded ORM/DB modules the agent worker lacks (D-00e): {leaked}"
