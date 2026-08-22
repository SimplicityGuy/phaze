"""Unit tests for the reachable / history-derived split in ``scripts/health_baseline.py``.

phaze-bk9el.1 criterion 3 is "split each file's total_deduction into REACHABLE and
HISTORY-DERIVED, with a repo-wide total for each". The whole point of that split is that roughly
two thirds of this repo's health deduction comes from biomarkers computed off git history, which
no refactor can move -- so the epic's final ledger reads as "the refactoring achieved nothing"
unless the split is there and is correct.

That makes exactly one failure mode expensive, and it is a SILENT one: a biomarker landing in the
wrong bucket, or a residual biomarker quietly folded into a named bucket, changes the headline
percentage without changing anything a reader can see. So the assertions below are on the bucket
assignment itself and on the invariant that the four buckets sum to the total -- the property that
makes an unnoticed reclassification impossible to hide.

The findings are synthetic dicts in ``load_findings``'s shape rather than a fixture index: the
classification is pure, and a test that needed a populated ``.repowise/wiki.db`` would be
unrunnable in CI (the index is gitignored and per-checkout).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from types import ModuleType


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "health_baseline.py"


def _load() -> ModuleType:
    assert _SCRIPT.is_file(), f"health baseline script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("health_baseline", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def health_baseline() -> ModuleType:
    return _load()


def _finding(biomarker_type: str, dimension: str, impact: float, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "biomarker_type": biomarker_type,
        "dimension": dimension,
        "health_impact": impact,
        "function_name": "do_thing",
        "line_start": 10,
        "line_end": 40,
        "details": details or {},
    }


def _metric(path: str = "src/phaze/example.py") -> dict[str, Any]:
    return {
        "file_path": path,
        "score": 4.0,
        "defect_score": 4.0,
        "maintainability_score": 7.0,
        "performance_score": 10.0,
        "line_coverage_pct": 98.0,
        "branch_coverage_pct": None,
        "max_ccn": 11,
        "max_nesting": 4,
        "nloc": 700,
        "duplication_pct": 5.0,
    }


@pytest.mark.parametrize(
    ("biomarker_type", "expected"),
    [
        # The nine the epic named reachable. `error_handling`, `primitive_obsession`,
        # `dry_violation` and `low_cohesion` are maintainability-dimension, not defect --
        # a split that only looked at the defect dimension would drop all four.
        ("dry_violation", "reachable"),
        ("error_handling", "reachable"),
        ("primitive_obsession", "reachable"),
        ("complex_method", "reachable"),
        ("nested_complexity", "reachable"),
        ("large_method", "reachable"),
        ("bumpy_road", "reachable"),
        ("low_cohesion", "reachable"),
        ("coverage_gradient", "reachable"),
        # The eight the epic named history-derived.
        ("prior_defect", "history_derived"),
        ("function_hotspot", "history_derived"),
        ("change_entropy", "history_derived"),
        ("co_change_scatter", "history_derived"),
        ("knowledge_loss", "history_derived"),
        ("churn_risk", "history_derived"),
        ("hidden_coupling", "history_derived"),
        ("ownership_risk", "history_derived"),
        # Performance is its own bucket: reachable in principle, but not this epic's surface.
        ("io_in_loop", "performance"),
        ("serial_await_in_loop", "performance"),
        # The residue the epic did not name. It must NOT land in a named bucket.
        ("complex_conditional", "unclassified"),
        ("brain_method", "unclassified"),
        ("a_biomarker_repowise_has_not_shipped_yet", "unclassified"),
    ],
)
def test_every_biomarker_lands_in_the_bucket_the_epic_named(health_baseline: ModuleType, biomarker_type: str, expected: str) -> None:
    assert health_baseline.classify_reachability(biomarker_type) == expected


def test_the_four_buckets_sum_to_the_total_deduction(health_baseline: ModuleType) -> None:
    """The invariant that makes a silent reclassification impossible.

    If a residual biomarker were folded into `reachable` or dropped on the floor, the headline
    percentage would move and nothing else would. Summing to the total is the property a reader
    can check against the artifact without re-running anything.
    """
    findings = [
        _finding("complex_method", "defect", 1.25),
        _finding("error_handling", "maintainability", 0.5),
        _finding("prior_defect", "defect", 0.75),
        _finding("change_entropy", "defect", 1.1),
        _finding("io_in_loop", "performance", 0.0),
        _finding("complex_conditional", "defect", 0.4),
    ]
    record = health_baseline.build_file_record(_metric(), findings, {})
    deduction = record["deduction"]

    assert deduction["reachable"] == pytest.approx(1.75)
    assert deduction["history_derived"] == pytest.approx(1.85)
    assert deduction["performance"] == pytest.approx(0.0)
    assert deduction["unclassified"] == pytest.approx(0.4)
    assert deduction["total"] == pytest.approx(sum(f["health_impact"] for f in findings))
    assert deduction["total"] == pytest.approx(
        deduction["reachable"] + deduction["history_derived"] + deduction["performance"] + deduction["unclassified"]
    )


def test_the_split_weighs_impact_rather_than_counting_findings(health_baseline: ModuleType) -> None:
    """Eight cheap findings must not outweigh one expensive one just by being numerous.

    This is why the baseline reports deduction and not the finding counts phaze-vu88k.1 reported:
    on `services/review.py` eight `error_handling` findings carry 0.496 points between them while a
    single `change_entropy` carries 1.097, and a count-based split inverts which bucket dominates.
    """
    findings = [*[_finding("error_handling", "maintainability", 0.062) for _ in range(8)], _finding("change_entropy", "defect", 1.097)]
    record = health_baseline.build_file_record(_metric(), findings, {})

    assert record["findings_by_biomarker"] == {"change_entropy": 1, "error_handling": 8}
    assert record["deduction"]["history_derived"] > record["deduction"]["reachable"]


def test_a_dry_violation_carries_its_clone_partner_and_shared_line_count(health_baseline: ModuleType) -> None:
    """Criterion 4: the DRY beads must not have to re-derive the clone pairs."""
    findings = [
        _finding(
            "dry_violation",
            "maintainability",
            0.25,
            {"duplication_pct": 16.92, "clone_pair_count": 3, "worst_clone_lines": 32, "worst_clone_partner": "src/phaze/routers/agent_metadata.py"},
        ),
        _finding("complex_method", "defect", 1.0),
    ]
    record = health_baseline.build_file_record(_metric(), findings, {})

    assert len(record["dry_violations"]) == 1
    dry = record["dry_violations"][0]
    assert dry["worst_clone_partner"] == "src/phaze/routers/agent_metadata.py"
    assert dry["worst_clone_lines"] == 32
    # `clone_pair_count` > 1 means repowise summarised further pairs it does NOT name; the record
    # has to carry the count so a reader knows the single partner is not the whole story.
    assert dry["clone_pair_count"] == 3
    assert dry["line_start"] == 10
    assert dry["line_end"] == 40


def test_a_file_with_no_branches_records_no_branch_percentage(health_baseline: ModuleType, tmp_path: Path) -> None:
    """A branch-less module is unmeasurable, not 0% and not 100%.

    Recording either number would make the file look like it had moved the moment a refactor adds
    its first branch -- the same reasoning `scripts/branch_coverage_check.py` applies, and the two
    artifacts have to agree or the per-bead gate and the ledger disagree about the same file.
    """
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    "src/phaze/straight_line.py": {
                        "summary": {
                            "num_statements": 10,
                            "covered_lines": 10,
                            "num_branches": 0,
                            "covered_branches": 0,
                            "percent_statements_covered": 100.0,
                            "percent_covered": 100.0,
                        }
                    },
                    "src/phaze/branchy.py": {
                        "summary": {
                            "num_statements": 20,
                            "covered_lines": 19,
                            "num_branches": 8,
                            "covered_branches": 6,
                            "percent_statements_covered": 95.0,
                            "percent_branches_covered": 75.0,
                            "percent_covered": 89.29,
                        }
                    },
                },
                "totals": {
                    "num_statements": 30,
                    "covered_lines": 29,
                    "num_branches": 8,
                    "covered_branches": 6,
                    "percent_statements_covered": 96.67,
                    "percent_branches_covered": 75.0,
                    "percent_covered": 92.11,
                },
            }
        ),
        encoding="utf-8",
    )

    coverage = health_baseline.load_coverage_json(report)

    assert coverage["src/phaze/straight_line.py"]["percent_branches_covered"] is None
    assert coverage["src/phaze/branchy.py"]["percent_branches_covered"] == pytest.approx(75.0)
    assert coverage["__totals__"]["percent_statements_covered"] == pytest.approx(96.67)


def test_the_repo_wide_summary_totals_both_named_buckets(health_baseline: ModuleType) -> None:
    """Criterion 3's second half: a repo-wide total for each bucket, not only per file."""
    records = [
        health_baseline.build_file_record(
            _metric("src/phaze/a.py"), [_finding("complex_method", "defect", 2.0), _finding("prior_defect", "defect", 3.0)], {}
        ),
        health_baseline.build_file_record(
            _metric("src/phaze/b.py"), [_finding("dry_violation", "maintainability", 1.0), _finding("churn_risk", "defect", 1.0)], {}
        ),
    ]
    summary = health_baseline.build_summary(records)

    assert summary["deduction_total"]["reachable"] == pytest.approx(3.0)
    assert summary["deduction_total"]["history_derived"] == pytest.approx(4.0)
    assert summary["deduction_total"]["total"] == pytest.approx(7.0)
    assert summary["reachable_pct_of_total"] == pytest.approx(42.86, abs=0.01)
    assert summary["history_derived_pct_of_total"] == pytest.approx(57.14, abs=0.01)
    assert summary["dry_violation_count"] == 1
