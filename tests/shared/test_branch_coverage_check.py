"""Unit tests for the per-bead branch-coverage gate (phaze-bk9el.21).

``scripts/branch_coverage_check.py`` is what `just branch-check` runs, and it is the ONLY place
branch coverage is gated in this repo: the repo-wide floors stay on lines (95% total, 90% per
module -- ``scripts/coverage_floor.py``) by operator decision, because branch coverage sits below
the line figure on most files here and a repo-wide branch gate would fail on day one.

That makes this script's failure modes load-bearing in a specific way. A refactor keeps every LINE
executing and changes only the branch combinations, so if this check reports green for the wrong
reason -- a report with no branch data, a baseline that quietly did not load, a touched file that
never made it into the checked set -- the epic loses the only signal able to see the regression it
is most likely to cause. The tests below are mostly about those false-green paths.

Fixtures are synthetic ``coverage.json`` / baseline files and the real ``main()`` over its real
argv interface; assertions are on the EXIT CODE and the reported text, never on internals.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "branch_coverage_check.py"


def _load() -> ModuleType:
    assert _SCRIPT.is_file(), f"branch check script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("branch_coverage_check", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _summary(*, num_branches: int, covered: int, statements: int = 100) -> dict[str, float]:
    pct = 0.0 if num_branches == 0 else covered / num_branches * 100.0
    return {
        "num_statements": statements,
        "missing_lines": 0,
        "percent_statements_covered": 100.0,
        "num_branches": num_branches,
        "covered_branches": covered,
        "percent_branches_covered": pct,
    }


def _write_coverage(tmp_path: Path, files: dict[str, dict[str, object]], *, branch_data: bool = True) -> Path:
    totals: dict[str, float] = {"num_statements": 100, "percent_statements_covered": 100.0}
    if branch_data:
        totals |= {"num_branches": 10, "covered_branches": 10, "percent_branches_covered": 100.0}
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"files": files, "totals": totals}), encoding="utf-8")
    return path


def _write_baseline(tmp_path: Path, files: dict[str, float]) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"files": {p: {"percent_branches_covered": v, "num_branches": 10} for p, v in files.items()}}), encoding="utf-8")
    return path


def _run(module: ModuleType, coverage: Path, *args: str) -> int:
    return module.main(["--coverage", str(coverage), *args])


def test_a_file_that_lowers_branch_coverage_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Criterion 5, the rule itself: lowering branch coverage on a touched file fails the bead."""
    module = _load()
    coverage = _write_coverage(
        tmp_path, {"src/phaze/core/job_runner.py": {"summary": _summary(num_branches=46, covered=39), "missing_branches": [[120, 121], [204, 0]]}}
    )
    baseline = _write_baseline(tmp_path, {"src/phaze/core/job_runner.py": 89.13})

    exit_code = _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/core/job_runner.py")

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "src/phaze/core/job_runner.py" in out
    assert "lowered branch coverage" in out


def test_holding_steady_and_raising_both_pass(tmp_path: Path) -> None:
    """ "Raising is welcome, holding steady is acceptable" -- both must be exit 0, not just raising."""
    module = _load()
    coverage = _write_coverage(
        tmp_path,
        {
            "src/phaze/a.py": {"summary": _summary(num_branches=10, covered=9), "missing_branches": [[3, 4]]},
            "src/phaze/b.py": {"summary": _summary(num_branches=10, covered=10), "missing_branches": []},
        },
    )
    baseline = _write_baseline(tmp_path, {"src/phaze/a.py": 90.0, "src/phaze/b.py": 80.0})

    assert _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/a.py", "--file", "src/phaze/b.py") == 0


def test_uncovered_branch_line_numbers_are_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Criterion 6: a percentage tells a developer they regressed and nothing about where.

    coverage.json records each uncovered arc as ``[source_line, destination]``; the source lines
    are what a developer can act on, deduplicated (one line can have several untaken arcs).
    """
    module = _load()
    coverage = _write_coverage(
        tmp_path, {"src/phaze/a.py": {"summary": _summary(num_branches=10, covered=7), "missing_branches": [[42, 43], [42, 50], [77, -1]]}}
    )
    baseline = _write_baseline(tmp_path, {"src/phaze/a.py": 90.0})

    _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/a.py")

    assert "uncovered branch lines: 42, 77" in capsys.readouterr().out


def test_a_report_without_branch_data_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The worst false green: no branch data reads as "no regressions" for every file.

    Exit 2 (broken measurement), never 0, so a run that forgot branch coverage cannot be mistaken
    for a clean bead.
    """
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/a.py": {"summary": {"num_statements": 10}}}, branch_data=False)

    with pytest.raises(SystemExit) as excinfo:
        _run(module, coverage, "--file", "src/phaze/a.py")

    assert excinfo.value.code == 2
    assert "no branch data" in capsys.readouterr().out


def test_a_missing_coverage_report_fails_closed(tmp_path: Path) -> None:
    """No coverage.json is a broken measurement, not an all-clear."""
    module = _load()

    with pytest.raises(SystemExit) as excinfo:
        _run(module, tmp_path / "absent.json", "--file", "src/phaze/a.py")

    assert excinfo.value.code == 2


def test_a_missing_baseline_fails_closed_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The rule: if the check could not perform a comparison, it did not pass.

    The tempting design is to exit 0 with a warning banner, because the baseline is produced by
    phaze-bk9el.1 which must itself pass this check. That is the one shape this epic cannot
    afford: ``exit 0`` is what `just check` and CI consume, a banner is for a human reading a
    transcript, and nobody reads banners in a 20-minute log. "Emit a success signal that measured
    nothing" is what phaze-jnj90 (a gate producing no coverage) and phaze-nqawu (a submit running
    no tests) both were. Twelve wave-2 beads run this check, and from inside the tool a baseline
    that legitimately does not exist yet is indistinguishable from one missing for a mundane
    reason -- a bad fetch, a branch without the file, a worktree cut before .1 landed.
    """
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/a.py": {"summary": _summary(num_branches=10, covered=5), "missing_branches": [[9, 10]]}})

    with pytest.raises(SystemExit) as excinfo:
        _run(module, coverage, "--baseline", str(tmp_path / "absent.json"), "--file", "src/phaze/a.py")

    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "No branch-coverage baseline" in out
    assert "--allow-missing-baseline" in out


def test_the_missing_baseline_exemption_is_explicit_at_the_call_site(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """phaze-bk9el.1's exemption is a flag a reviewer can see, not a default nobody can.

    An exemption written at the call site is auditable; one baked into the default is invisible to
    every bead downstream. Anyone who later finds --allow-missing-baseline in a wave-2 bead knows
    immediately that something is wrong.
    """
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/a.py": {"summary": _summary(num_branches=10, covered=5), "missing_branches": [[9, 10]]}})

    exit_code = _run(module, coverage, "--baseline", str(tmp_path / "absent.json"), "--file", "src/phaze/a.py", "--allow-missing-baseline")

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "NOT A COMPARISON" in out
    assert "50.00%" in out


def test_a_file_absent_from_the_baseline_is_new_and_cannot_regress(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A file this bead created has nothing to be measured against."""
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/new.py": {"summary": _summary(num_branches=10, covered=4), "missing_branches": []}})
    baseline = _write_baseline(tmp_path, {"src/phaze/old.py": 99.0})

    assert _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/new.py") == 0
    assert "🆕" in capsys.readouterr().out


def test_a_branchless_file_is_not_reported_as_zero_percent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A file with no branches is not 0% and not 100% -- it is not measurable.

    Reporting either number would make it look like it had moved the moment a refactor adds its
    first branch, which is a false regression on exactly the change class this gate watches.
    """
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/constants.py": {"summary": _summary(num_branches=0, covered=0), "missing_branches": []}})
    baseline = _write_baseline(tmp_path, {"src/phaze/constants.py": 100.0})

    assert _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/constants.py") == 0
    assert "no branches to measure" in capsys.readouterr().out


def test_a_hundredth_of_a_point_is_not_a_regression(tmp_path: Path) -> None:
    """Two percentages computed from different runs are not float-equal; the gate is not that brittle."""
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/a.py": {"summary": _summary(num_branches=10000, covered=8912), "missing_branches": []}})
    baseline = _write_baseline(tmp_path, {"src/phaze/a.py": 89.1234})

    assert _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/a.py") == 0


def test_touched_files_are_named_in_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Criterion 4: "it must name the files it checked" -- a bare verdict is not actionable."""
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/a.py": {"summary": _summary(num_branches=10, covered=10), "missing_branches": []}})
    baseline = _write_baseline(tmp_path, {"src/phaze/a.py": 100.0})

    _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/a.py")
    out = capsys.readouterr().out

    assert "1 tracked file(s) this bead touched" in out
    assert "src/phaze/a.py" in out


def test_write_baseline_records_every_module_under_all_three_metrics(tmp_path: Path) -> None:
    """The baseline is phaze-bk9el.1's published per-module split, not just this gate's input.

    All three metrics are stored because they are three different numbers once branch coverage is
    on, and because .1 should not have to re-derive them from a second 20-minute suite. A module
    with no branches is still recorded -- it has statement coverage worth publishing -- but its
    branch figure is null rather than 0% or 100%, so the comparison skips it instead of reading a
    fabricated number.
    """
    module = _load()
    coverage = _write_coverage(
        tmp_path,
        {
            "src/phaze/a.py": {"summary": _summary(num_branches=10, covered=9), "missing_branches": []},
            "src/phaze/constants.py": {"summary": _summary(num_branches=0, covered=0), "missing_branches": []},
        },
    )
    baseline = tmp_path / "written.json"

    assert _run(module, coverage, "--baseline", str(baseline), "--write-baseline") == 0
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert set(recorded["files"]) == {"src/phaze/a.py", "src/phaze/constants.py"}
    assert recorded["files"]["src/phaze/a.py"]["percent_branches_covered"] == pytest.approx(90.0)
    assert recorded["files"]["src/phaze/constants.py"]["percent_branches_covered"] is None
    assert recorded["files"]["src/phaze/constants.py"]["percent_statements_covered"] == pytest.approx(100.0)
    assert recorded["totals"] is not None


def test_a_branchless_baseline_entry_does_not_read_as_a_regression(tmp_path: Path) -> None:
    """A null branch figure in the baseline must not be compared against as if it were a number."""
    module = _load()
    coverage = _write_coverage(tmp_path, {"src/phaze/constants.py": {"summary": _summary(num_branches=0, covered=0), "missing_branches": []}})
    baseline = tmp_path / "b.json"
    baseline.write_text(json.dumps({"files": {"src/phaze/constants.py": {"percent_branches_covered": None}}}), encoding="utf-8")

    assert _run(module, coverage, "--baseline", str(baseline), "--file", "src/phaze/constants.py") == 0
