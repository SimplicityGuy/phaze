"""Find multi-operand boolean guards: the blind spot in arc-level branch coverage (phaze-5f1sv).

coverage.py records branch arcs per *decision statement*, not per boolean operand. ``if A or B or C:``
contributes exactly two arcs (taken, not taken) however many conditions it fuses, so a defence
behind ``C`` that no test ever reaches leaves the ``if`` fully covered, the file fully covered and
``just branch-check`` green. Measured on the finding's own file: the fused guard read 73/74
(98.65%); splitting its third disjunct into its own ``if`` read 74/76 (97.37%), exposing the
uncovered arc.

DECISION (the bead's criterion 1, option (a), report only). The repo does NOT gate on this:

  * (b) measuring condition coverage / MC-DC is not available -- coverage.py is arc-level only and
    has no condition-coverage mode -- so it would mean a custom AST instrumenter in the test run,
    which is prohibitive for a single-operator repo and would half-adopt a second coverage system.
  * mass-splitting guards would lower measured coverage repo-wide for a measurement artifact.

Instead ``scripts/branch_coverage_check.py`` prints, per touched file, where the multi-operand
decisions are, so a green branch-check is not read as "every operand was exercised". It never
affects the exit code.

What counts: a ``BoolOp`` with two or more operands used as the test of ``if``/``while``/``assert``/
a conditional expression, or as a comprehension filter. ``any()``/``all()`` over a generator hides
operands the same way but is not a BoolOp; those are listed too when the argument is a generator
expression carrying a filter or a BoolOp.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class FusedGuard:
    """One decision statement whose arcs hide ``operands`` independent conditions."""

    line: int
    kind: str
    operands: int


def _bool_operands(node: ast.expr) -> int:
    """Operands of the outermost BoolOp in ``node`` (0 when ``node`` is not a BoolOp)."""
    return len(node.values) if isinstance(node, ast.BoolOp) else 0


def find_fused_guards(source: str) -> list[FusedGuard]:
    """Return every multi-operand decision in ``source``, in line order. Unparseable source yields none."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[FusedGuard] = []
    for node in ast.walk(tree):
        tests: list[tuple[ast.expr, str]] = []
        if isinstance(node, ast.If | ast.While | ast.Assert | ast.IfExp):
            tests.append((node.test, type(node).__name__.lower()))
        elif isinstance(node, ast.comprehension):
            tests.extend((cond, "comprehension filter") for cond in node.ifs)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("any", "all"):
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.GeneratorExp) and (any(g.ifs for g in arg.generators) or _bool_operands(arg.elt)):
                found.append(FusedGuard(node.lineno, f"{node.func.id}() over a generator", 1 + sum(len(g.ifs) for g in arg.generators)))
        for test, kind in tests:
            operands = _bool_operands(test)
            if operands >= 2:
                found.append(FusedGuard(test.lineno, kind, operands))
    return sorted(found, key=lambda guard: guard.line)


def describe(path: Path) -> str | None:
    """One-line context for ``path``, or None when it has no fused guard (or cannot be read)."""
    try:
        guards = find_fused_guards(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not guards:
        return None
    lines = ", ".join(f"{g.line} ({g.operands} operands)" for g in guards)
    return (
        f"{len(guards)} multi-operand guard(s) at line {lines}: coverage.py counts arcs per decision, so an untested "
        "operand inside one is NOT reflected in the branch figure above (phaze-5f1sv)"
    )
