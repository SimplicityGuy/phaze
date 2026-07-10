"""T-84-06-02: ``services/shadow_compare.py`` must stay read-only by construction.

The Phase-84 threat register claimed this threat was mitigated by "a destructive-write guard [that]
refuses any DB whose name does not end in ``_test``". **No such guard exists** anywhere in
``src/phaze/`` — the only ``_test``-suffix check is a ``pytest.skip`` in
``tests/integration/test_dedup_resolve_undo_shadow.py``, a test-suite guard, not a production control.

The control that is actually load-bearing is that :mod:`phaze.services.shadow_compare` performs **no
writes at all**: it issues only ``select()`` statements, and its CLI (``phaze/cli/shadow_compare.py``)
imports neither ``main`` nor Alembic. That property is what makes
``just shadow-compare --database-url <live-dsn>`` safe to point at a production database. It was
relied upon to run the Phase-84 live-corpus check against real production data. Nothing tested it.

This guard locks it. Adding a single ``session.add(...)``, ``session.commit()``, or an ``insert()`` /
``update()`` / ``delete()`` import to that module turns this test RED.

AST-based, deliberately. The module's own docstring and comments contain the words ``text()`` and
``update`` as *prose* (lines 21 and 66) while describing the anti-patterns it avoids — a line-oriented
``grep`` guard would false-positive on them. An ``ast.walk`` never sees prose. This is the same trap
that made two Phase-83 guards toothless.

DB-free; ``shared`` bucket.
"""

from __future__ import annotations

import ast
import pathlib


_SHADOW_SRC = pathlib.Path(__file__).resolve().parents[1] / ".." / "src" / "phaze" / "services" / "shadow_compare.py"

# SQLAlchemy names that can only be used to build a write, plus `text` (raw SQL escapes every
# structural check, and the module's own house style forbids it -- T-79-01).
FORBIDDEN_IMPORT_NAMES = frozenset({"insert", "update", "delete", "Insert", "Update", "Delete", "text"})

# Session methods that mutate. `execute` is absent on purpose: it is how selects are issued.
FORBIDDEN_SESSION_METHODS = frozenset(
    {"add", "add_all", "commit", "flush", "merge", "delete", "bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"}
)


def _tree() -> ast.Module:
    return ast.parse(_SHADOW_SRC.resolve().read_text(encoding="utf-8"))


def test_shadow_compare_imports_no_write_constructs() -> None:
    """The module never imports insert/update/delete/text -- it cannot build a write statement."""
    offenders: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom):
            offenders.extend(
                f"line {node.lineno}: from {node.module} import {alias.name}" for alias in node.names if alias.name in FORBIDDEN_IMPORT_NAMES
            )
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"line {node.lineno}: import {alias.name}" for alias in node.names if alias.name.rsplit(".", 1)[-1] in FORBIDDEN_IMPORT_NAMES
            )

    assert not offenders, (
        "services/shadow_compare.py imported a write construct. It is read-only by construction — that property "
        "is the real mitigation for T-84-06-02 and is what makes `just shadow-compare --database-url <live-dsn>` "
        f"safe against a production database. Offenders: {offenders}"
    )


def test_shadow_compare_calls_no_session_mutators() -> None:
    """The module never calls a mutating Session method (add/commit/flush/delete/...)."""
    offenders: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_SESSION_METHODS:
            offenders.append(f"line {node.lineno}: .{node.func.attr}(...)")

    assert not offenders, (
        f"services/shadow_compare.py calls a mutating Session method; it must issue only selects (T-84-06-02). Offenders: {offenders}"
    )
