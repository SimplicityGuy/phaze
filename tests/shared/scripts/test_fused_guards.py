"""``scripts/fused_guards.py``: the report-only context for arc-level coverage's blind spot (phaze-5f1sv).

coverage.py counts branch arcs per decision statement, so ``if A or B or C:`` is two arcs however many
operands it fuses. The detector exists so ``just branch-check`` can say where those decisions are; it
must find every shape the bead names and must never turn into a gate.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str) -> ModuleType:
    path = _ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # @dataclass resolves its module by name
    spec.loader.exec_module(module)
    return module


_FUSED = _load("fused_guards")

# The finding's own shape: three disjuncts, one `if`, two arcs.
_FINDING_GUARD = "if not isinstance(function, str) or function not in _KEY_BUILDERS or not isinstance(key, str):\n    pass\n"


def test_the_findings_three_disjunct_guard_is_reported_with_its_operand_count() -> None:
    assert _FUSED.find_fused_guards(_FINDING_GUARD) == [_FUSED.FusedGuard(line=1, kind="if", operands=3)]


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("if a and b:\n    pass\n", "if"),
        ("while a or b:\n    pass\n", "while"),
        ("assert a and b\n", "assert"),
        ("x = 1 if a or b else 2\n", "ifexp"),
        ("y = [i for i in xs if a(i) and b(i)]\n", "comprehension filter"),
    ],
)
def test_every_decision_position_is_covered(source: str, kind: str) -> None:
    (guard,) = _FUSED.find_fused_guards(source)
    assert (guard.kind, guard.operands) == (kind, 2)


@pytest.mark.parametrize(
    "source",
    ["if any(a(i) for i in xs if b(i)):\n    pass\n", "if all(a(i) and b(i) for i in xs):\n    pass\n"],
)
def test_any_and_all_over_a_filtered_generator_are_reported(source: str) -> None:
    assert any("generator" in g.kind for g in _FUSED.find_fused_guards(source))


@pytest.mark.parametrize("source", ["if a:\n    pass\n", "if not a:\n    pass\n", "x = a or b\n", "any(xs)\n", "def broken(:\n"])
def test_single_operand_decisions_plain_boolops_and_bad_source_are_not_reported(source: str) -> None:
    assert _FUSED.find_fused_guards(source) == []


def test_describe_names_the_line_and_the_blind_spot(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("x = 1\n" + _FINDING_GUARD, encoding="utf-8")
    text = _FUSED.describe(target)
    assert text is not None
    assert "line 2 (3 operands)" in text
    assert "NOT reflected" in text
    assert _FUSED.describe(tmp_path / "missing.py") is None


def test_branch_check_prints_the_context_and_it_never_changes_the_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Through the real ``main()``: a fully covered file with a fused guard still exits 0, and the note appears."""
    checker = _load("branch_coverage_check")
    probe = tmp_path / "probe.py"
    probe.write_text(_FINDING_GUARD, encoding="utf-8")
    target = str(probe)
    summary = {
        "num_statements": 2,
        "covered_lines": 2,
        "missing_lines": 0,
        "percent_statements_covered": 100.0,
        "num_branches": 2,
        "covered_branches": 2,
        "percent_branches_covered": 100.0,
    }
    coverage = tmp_path / "coverage.json"
    coverage.write_text(json.dumps({"files": {target: {"summary": summary, "missing_branches": []}}, "totals": summary}), encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"files": {target: {"percent_branches_covered": 100.0, "num_branches": 2}}}), encoding="utf-8")
    code = checker.main(["--coverage", str(coverage), "--baseline", str(baseline), "--file", target])
    out = capsys.readouterr().out
    assert code == 0
    assert "1 multi-operand guard(s) at line 1 (3 operands)" in out
