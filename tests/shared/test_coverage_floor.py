"""Unit tests for the per-module coverage floor gate (Phase 64-01, COV-01 / COV-02).

``scripts/coverage_floor.py`` is a CI-load-bearing script: it runs inside ``just coverage-combine``
on the COMBINED ``coverage.json`` and fails the required check if any tracked ``phaze/**`` module
falls below the uniform 90% floor. Because it gates merges, it gets its own tests -- the same
precedent as ``scripts/classify-changed-files.sh`` / ``tests/shared/test_change_gate.py`` (Phase 63).

These tests write synthetic ``coverage.json`` fixtures into ``tmp_path``, ``chdir`` into it, and
call the real ``main()`` over its real interface, asserting on the EXIT CODE (the observable
outcome, D-07). The load-bearing case is :func:`test_missing_coverage_json_fails_closed`: a missing
or unparseable ``coverage.json`` must NEVER exit 0 (T-64-01) -- a fail-open gate would let a
sub-floor module merge silently.

This file lives under ``tests/shared/`` so it rides the shared bucket in the Phase 63 test partition.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


# tests/shared/test_coverage_floor.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "coverage_floor.py"


def _load_floor_module() -> ModuleType:
    """Import the real ``scripts/coverage_floor.py`` as a module so we can call ``main()`` directly.

    ``scripts/`` is not an importable package, so load it from its file path. Exercising the real
    ``main()`` (rather than a copy) keeps these tests honest about the shipped gate's behaviour.
    """
    assert _SCRIPT.is_file(), f"floor script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("coverage_floor", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A repo-wide `totals` block that clears the 95% LINE floor, so a per-module test stays a test of
# the per-module floor. phaze-bk9el.21 added the repo-wide floor to this script; without a default
# every fixture below would trip it and stop measuring what it was written to measure.
_PASSING_TOTALS: dict[str, float] = {"num_statements": 1000, "missing_lines": 0, "percent_statements_covered": 100.0, "percent_covered": 100.0}


def _write_coverage_json(tmp_path: Path, files: dict[str, dict[str, float]], totals: dict[str, float] | None = None) -> None:
    """Write a synthetic ``coverage.json`` (only the fields the script reads).

    Summaries carry ``percent_statements_covered`` because that -- not ``percent_covered`` -- is
    what the gate reads since phaze-bk9el.21 enabled branch coverage: with ``branch = true``,
    coverage.py's ``percent_covered`` is the COMBINED line+branch figure, and the operator's
    decision is that phaze's floors stay on LINES.
    """
    payload = {
        "files": {path: {"summary": summary} for path, summary in files.items()},
        "totals": dict(_PASSING_TOTALS) if totals is None else totals,
    }
    (tmp_path / "coverage.json").write_text(json.dumps(payload), encoding="utf-8")


def test_sub_floor_module_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A module below 90% -> exit 1 AND its path is reported on stdout (case 1)."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {"src/phaze/services/review.py": {"num_statements": 95, "missing_lines": 16, "percent_statements_covered": 83.16}},
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 1
    assert "src/phaze/services/review.py" in capsys.readouterr().out


def test_all_modules_at_or_above_floor_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tracked module >= 90.0 -> exit 0 (case 2). Boundary 90.0 is not a failure."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {
            "src/phaze/main.py": {"num_statements": 40, "missing_lines": 0, "percent_statements_covered": 100.0},
            "src/phaze/services/pipeline.py": {"num_statements": 200, "missing_lines": 20, "percent_statements_covered": 90.0},
        },
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_zero_statement_module_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``num_statements == 0`` file reading 0% is skipped, not counted a failure (case 3)."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {
            "src/phaze/__init__.py": {"num_statements": 0, "missing_lines": 0, "percent_statements_covered": 0.0},
            "src/phaze/main.py": {"num_statements": 40, "missing_lines": 0, "percent_statements_covered": 100.0},
        },
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_exempt_module_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sub-floor module whose path is in EXEMPT is skipped -> exit 0 (case 4, D-09)."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {"src/phaze/services/review.py": {"num_statements": 95, "missing_lines": 16, "percent_statements_covered": 83.16}},
    )
    # Add the only sub-floor module to EXEMPT (with a justification, as D-09 requires).
    monkeypatch.setitem(module.EXEMPT, "src/phaze/services/review.py", "test: exercised via exempt path")
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_missing_coverage_json_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAIL CLOSED (T-64-01): no coverage.json -> raise (non-zero exit), NEVER exit 0 (case 5)."""
    module = _load_floor_module()
    monkeypatch.chdir(tmp_path)  # tmp_path is empty -> no coverage.json

    with pytest.raises(FileNotFoundError):
        module.main()


def test_empty_coverage_json_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAIL CLOSED (T-64-01): an empty/unparseable coverage.json -> raise, NEVER exit 0 (case 5)."""
    module = _load_floor_module()
    (tmp_path / "coverage.json").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(json.JSONDecodeError):
        module.main()


def test_empty_files_dict_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """FAIL CLOSED (T-64-01): a parseable ``{"files": {}}`` -> exit 1, NEVER exit 0 (WR-01).

    A report with zero tracked modules means the measurement produced nothing (e.g. no shards
    were combined). An empty loop must not be mistaken for an all-clear, or a broken combine
    would merge green with the gate never actually run against real coverage.
    """
    module = _load_floor_module()
    (tmp_path / "coverage.json").write_text(json.dumps({"files": {}, "totals": dict(_PASSING_TOTALS)}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert module.main() == 1
    assert "no tracked files" in capsys.readouterr().out


# --- phaze-bk9el.21: the floors are on LINES, and stayed there when branch coverage was enabled ---


def test_the_gate_reads_lines_not_the_combined_branch_figure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this script was rewritten to prevent.

    With ``branch = true``, coverage.py's ``percent_covered`` becomes
    ``(covered_lines + covered_branches) / (num_statements + num_branches)`` -- verified against
    coverage 7.15.4, which offers no option to make ``fail_under`` line-only. A module at 97% lines
    and 60% branches reads 88% combined, and would have started failing a 90% floor that nobody
    raised. The operator's decision on phaze-bk9el.21 is that the repo-wide gate stays on lines, so
    reading ``percent_covered`` here is the defect, not a detail.
    """
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {
            "src/phaze/services/analysis.py": {
                "num_statements": 400,
                "missing_lines": 12,
                "percent_statements_covered": 97.0,
                "num_branches": 116,
                "covered_branches": 70,
                "percent_branches_covered": 60.34,
                "percent_covered": 88.45,  # combined -- below the 90% floor, and NOT what is read
            }
        },
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_repo_wide_line_floor_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The 95% repo-wide floor moved here from pyproject's ``fail_under``; it must still fire."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {"src/phaze/main.py": {"num_statements": 1000, "missing_lines": 60, "percent_statements_covered": 94.0}},
        totals={"num_statements": 1000, "missing_lines": 60, "percent_statements_covered": 94.0, "percent_covered": 91.0},
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "Repo-wide LINE coverage 94.00%" in out
    assert "95% floor" in out


def test_repo_wide_floor_reads_lines_not_the_combined_figure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo whose LINES clear 95% but whose COMBINED figure does not must still pass.

    This is the whole point of criterion 3: enabling branch measurement must not tighten the
    repo-wide gate by the back door. 98.78% lines / 88% combined is close to this repo's own
    shape, and it passes.
    """
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {"src/phaze/main.py": {"num_statements": 1000, "missing_lines": 12, "percent_statements_covered": 98.78}},
        totals={"num_statements": 1000, "missing_lines": 12, "percent_statements_covered": 98.78, "percent_covered": 88.0},
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_missing_totals_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A report with no ``totals`` leaves the repo-wide floor unarmed -> exit 1, never 0."""
    module = _load_floor_module()
    (tmp_path / "coverage.json").write_text(
        json.dumps({"files": {"src/phaze/main.py": {"summary": {"num_statements": 10, "missing_lines": 0, "percent_statements_covered": 100.0}}}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 1
    assert "no 'totals' section" in capsys.readouterr().out


def test_line_percent_falls_back_to_the_ratio_on_an_older_report(tmp_path: Path) -> None:
    """``percent_statements_covered`` arrived in coverage.py 7.6; the fallback yields the same number."""
    module = _load_floor_module()

    assert module.line_percent({"num_statements": 200, "covered_lines": 190}) == pytest.approx(95.0)
    assert module.line_percent({"num_statements": 0, "covered_lines": 0}) == pytest.approx(100.0)
