#!/usr/bin/env python3
"""Emit a per-file Repowise health/performance baseline for every file under ``src/``.

`repowise health` only prints the worst 20 files; the full per-file data lives in the
Repowise sqlite index (``.repowise/wiki.db``, tables ``health_file_metrics`` and
``health_findings``). This script reads both tables directly and emits, for every
``src/`` file: path, score, defect_score, maintainability_score, performance_score,
line_coverage_pct, and the count of open findings by biomarker_type and by dimension.

It also applies the historical/structural/performance biomarker split established for
epic phaze-vu88k, so a downstream refactor bead can see how much of a file's defect
deduction is actually reachable by code change before it starts:

  * STRUCTURAL (defect dimension, reachable by refactor):
    complex_method, nested_complexity, large_method, bumpy_road
  * HISTORICAL (defect dimension, decays only as the file stays stable — no refactor
    removes these): prior_defect, function_hotspot, change_entropy, co_change_scatter,
    knowledge_loss, churn_risk, hidden_coupling
  * Any other defect-dimension biomarker_type (e.g. coverage_gradient,
    complex_conditional) is reported separately as "other_defect" rather than folded
    into either bucket — see the committed baseline artifact for why.
  * PERFORMANCE biomarkers (io_in_loop, serial_await_in_loop, nested_loop_with_io,
    hot_path_sync_io, membership_test_against_list_in_loop) feed the performance
    dimension only; they are reported per file and in the summary but are not part of
    the defect split.

Usage:
    uv run scripts/health_baseline.py [--db PATH] [--repo NAME] [--format json|csv] [--out PATH]

The default --db is ``<repo-root>/.repowise/wiki.db`` (resolved relative to this
script's own location, not the caller's cwd), so the same invocation is reproducible
from any worktree that has its own Repowise index. ``.repowise/`` is gitignored -- a
worktree that has never run `repowise init` will not have one; point --db at another
checkout's index (e.g. the primary worktree) to reproduce the committed baseline.

Output is deterministic (files sorted by path, biomarker/dimension keys sorted) so two
runs can be diffed directly with `diff` or `jq -S . a.json | diff - <(jq -S . b.json)`.

phaze-bk9el.1 EXTENSION — deductions, not just counts
-----------------------------------------------------
Epic phaze-bk9el needs a strictly larger baseline than phaze-vu88k did, and the extra
data is additive: every key the vu88k artifact carried is still emitted with the same
meaning, so the two artifacts stay diffable on their shared keys.

What was added:

  * The remaining ``health_file_metrics`` columns — ``max_ccn``, ``max_nesting``,
    ``nloc``, ``duplication_pct``, ``branch_coverage_pct``.
  * DEDUCTION, not finding counts. ``health_findings.health_impact`` is the score
    deduction each finding carries; counting findings treats a 1.10-point
    ``change_entropy`` and a 0.06-point ``error_handling`` as equal, which they are not.
    Every file gets ``deduction_split`` (reachable / history_derived / performance /
    unclassified) and ``deduction_by_biomarker``, and the summary totals both repo-wide.
  * The REACHABLE vs HISTORY-DERIVED split epic phaze-bk9el defines, which is broader
    than vu88k's structural/historical one — it counts ``dry_violation``,
    ``error_handling``, ``primitive_obsession``, ``low_cohesion`` and
    ``coverage_gradient`` as reachable, where vu88k left the last one undetermined.
    BOTH classifications are emitted; neither replaces the other.
  * Per-file ``dry_violations``, carrying each finding's clone partner and shared line
    count straight out of ``details_json``, so the DRY beads do not re-derive them.
  * ``--coverage-json``: merge authoritative statement/branch coverage from a
    ``coverage json`` report. Repowise's own ``line_coverage_pct`` is ingested from
    Cobertura and its ``branch_coverage_pct`` may be null; the coverage.py report is
    the source of truth for both and is what the per-bead branch gate compares against.

ON "TOTAL DEDUCTION". ``deduction.total`` is the sum of ``health_impact`` over a file's
open findings. It is NOT ``10 - score``: repowise scores each DIMENSION separately and
the headline score is not a flat subtraction, so the split below is a faithful
attribution of finding impact, not an arithmetic decomposition of the score. Use it to
compare buckets against each other, which is the question the epic actually asks.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / ".repowise" / "wiki.db"

STRUCTURAL_BIOMARKERS = frozenset(
    {
        "complex_method",
        "nested_complexity",
        "large_method",
        "bumpy_road",
    }
)

HISTORICAL_BIOMARKERS = frozenset(
    {
        "prior_defect",
        "function_hotspot",
        "change_entropy",
        "co_change_scatter",
        "knowledge_loss",
        "churn_risk",
        "hidden_coupling",
    }
)

PERFORMANCE_BIOMARKERS = frozenset(
    {
        "io_in_loop",
        "serial_await_in_loop",
        "nested_loop_with_io",
        "hot_path_sync_io",
        "membership_test_against_list_in_loop",
    }
)

# --- epic phaze-bk9el's split (see the module docstring) -----------------------------------------
# REACHABLE: a refactor of this file can remove the finding.
REACHABLE_BIOMARKERS = frozenset(
    {
        "bumpy_road",
        "complex_method",
        "coverage_gradient",
        "dry_violation",
        "error_handling",
        "large_method",
        "low_cohesion",
        "nested_complexity",
        "primitive_obsession",
    }
)

# HISTORY-DERIVED: computed from git history. No edit to the file removes these; they decay only
# as the file stays stable. Roughly two thirds of this repo's deduction sits here, which is why a
# ledger that reports only the headline score reads as though refactoring achieved nothing.
HISTORY_DERIVED_BIOMARKERS = frozenset(
    {
        "change_entropy",
        "churn_risk",
        "co_change_scatter",
        "function_hotspot",
        "hidden_coupling",
        "knowledge_loss",
        "ownership_risk",
        "prior_defect",
    }
)

METRIC_COLUMNS = (
    "file_path",
    "score",
    "defect_score",
    "maintainability_score",
    "performance_score",
    "line_coverage_pct",
    "branch_coverage_pct",
    "max_ccn",
    "max_nesting",
    "nloc",
    "duplication_pct",
)

BELOW_FLOOR_THRESHOLD = 6.0


def classify_defect_biomarker(biomarker_type: str) -> str:
    """Bucket one defect-dimension biomarker into structural / historical / other_defect (vu88k)."""
    if biomarker_type in STRUCTURAL_BIOMARKERS:
        return "structural"
    if biomarker_type in HISTORICAL_BIOMARKERS:
        return "historical"
    return "other_defect"


def classify_reachability(biomarker_type: str) -> str:
    """Bucket ANY biomarker into epic phaze-bk9el's reachable / history_derived / performance / unclassified.

    Unlike ``classify_defect_biomarker`` this covers every dimension, because the epic's question is
    "how much of this file's deduction can a refactor actually move", and the maintainability
    biomarkers (``dry_violation``, ``error_handling``, ``primitive_obsession``, ``low_cohesion``) are
    among the most reachable findings in the tree.

    Anything the epic did not name lands in ``unclassified`` and is reported there rather than being
    folded into whichever bucket makes the headline nicer — the same discipline phaze-vu88k.1 applied
    to its own residual, and for the same reason: a downstream bead should decide the disposition on
    the evidence rather than inherit an assumption.
    """
    if biomarker_type in REACHABLE_BIOMARKERS:
        return "reachable"
    if biomarker_type in HISTORY_DERIVED_BIOMARKERS:
        return "history_derived"
    if biomarker_type in PERFORMANCE_BIOMARKERS:
        return "performance"
    return "unclassified"


def resolve_repository(conn: sqlite3.Connection, repo_name: str) -> tuple[str, str | None]:
    """Return (repository_id, head_commit) for the named repository."""
    row = conn.execute(
        "SELECT id, head_commit FROM repositories WHERE name = ?",
        (repo_name,),
    ).fetchone()
    if row is None:
        available = [r[0] for r in conn.execute("SELECT name FROM repositories").fetchall()]
        raise SystemExit(f"no repository named {repo_name!r} in index; available: {available}")
    return row[0], row[1]


def load_metrics(conn: sqlite3.Connection, repo_id: str) -> list[dict[str, Any]]:
    """Every src/ file's row from health_file_metrics, sorted by path."""
    rows = conn.execute(
        f"""
        SELECT {", ".join(METRIC_COLUMNS)}
        FROM health_file_metrics
        WHERE repository_id = ? AND file_path LIKE 'src/%'
        ORDER BY file_path
        """,  # noqa: S608  # nosec B608 - column names are a module-level literal tuple, not input
        (repo_id,),
    ).fetchall()
    return [dict(zip(METRIC_COLUMNS, row, strict=True)) for row in rows]


def load_findings(conn: sqlite3.Connection, repo_id: str) -> dict[str, list[dict[str, Any]]]:
    """file_path -> one dict per open src/ finding, carrying its impact and raw details.

    ``health_impact`` is the score deduction the finding carries and is what phaze-bk9el.1 splits;
    ``details_json`` is what makes the DRY findings actionable (clone partner, shared line count)
    without a second tool run. Ordered so the emitted records are deterministic.
    """
    rows = conn.execute(
        """
        SELECT file_path, biomarker_type, dimension, health_impact, function_name, line_start, line_end, details_json
        FROM health_findings
        WHERE repository_id = ? AND file_path LIKE 'src/%' AND status = 'open'
        ORDER BY file_path, biomarker_type, line_start, line_end
        """,
        (repo_id,),
    ).fetchall()
    findings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for file_path, biomarker_type, dimension, health_impact, function_name, line_start, line_end, details_json in rows:
        findings[file_path].append(
            {
                "biomarker_type": biomarker_type,
                "dimension": dimension or "unknown",
                "health_impact": float(health_impact),
                "function_name": function_name,
                "line_start": line_start,
                "line_end": line_end,
                "details": _parse_details(details_json),
            }
        )
    return findings


def _parse_details(details_json: str | None) -> dict[str, Any]:
    """``details_json`` as a dict; ``{}`` when absent or not an object.

    A finding whose details do not parse is a data problem, not a reason to abort a read-only
    baseline — the finding still counts toward the deduction, it just contributes no clone detail.
    """
    if not details_json:
        return {}
    try:
        parsed = json.loads(details_json)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dry_violation_records(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Criterion 4: every ``dry_violation`` on this file, with its clone partner and shared lines.

    Repowise records ONE partner per finding — the worst clone pair — alongside ``clone_pair_count``
    for how many pairs that finding summarises. Where the count exceeds 1 the other partners are not
    in the index at all, so this reports what exists and says so rather than implying completeness.
    """
    records = []
    for finding in findings:
        if finding["biomarker_type"] != "dry_violation":
            continue
        details = finding["details"]
        records.append(
            {
                "clone_pair_count": details.get("clone_pair_count"),
                "duplication_pct": details.get("duplication_pct"),
                "function_name": finding["function_name"],
                "health_impact": round(finding["health_impact"], 4),
                "line_end": finding["line_end"],
                "line_start": finding["line_start"],
                "worst_clone_co_change": details.get("worst_clone_co_change"),
                "worst_clone_lines": details.get("worst_clone_lines"),
                "worst_clone_partner": details.get("worst_clone_partner"),
            }
        )
    return records


REACHABILITY_BUCKETS = ("reachable", "history_derived", "performance", "unclassified")


def build_file_record(metric: dict[str, Any], findings: list[dict[str, Any]], coverage: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_biomarker: dict[str, int] = defaultdict(int)
    by_dimension: dict[str, int] = defaultdict(int)
    defect_split = {"structural": 0, "historical": 0, "other_defect": 0}
    deduction_split = dict.fromkeys(REACHABILITY_BUCKETS, 0.0)
    deduction_by_biomarker: dict[str, float] = defaultdict(float)
    for finding in findings:
        biomarker_type = finding["biomarker_type"]
        dimension = finding["dimension"]
        impact = finding["health_impact"]
        by_biomarker[biomarker_type] += 1
        by_dimension[dimension] += 1
        if dimension == "defect":
            defect_split[classify_defect_biomarker(biomarker_type)] += 1
        deduction_split[classify_reachability(biomarker_type)] += impact
        deduction_by_biomarker[biomarker_type] += impact

    record = dict(metric)
    record["path"] = record.pop("file_path")
    record["findings_by_biomarker"] = dict(sorted(by_biomarker.items()))
    record["findings_by_dimension"] = dict(sorted(by_dimension.items()))
    record["defect_split"] = defect_split
    record["total_findings"] = sum(by_biomarker.values())
    # reorder so "path" leads, matching the acceptance criteria's field order
    ordered = {"path": record["path"]}
    ordered.update({k: record[k] for k in METRIC_COLUMNS[1:]})
    ordered["findings_by_biomarker"] = record["findings_by_biomarker"]
    ordered["findings_by_dimension"] = record["findings_by_dimension"]
    ordered["defect_split"] = record["defect_split"]
    ordered["total_findings"] = record["total_findings"]
    ordered["deduction"] = {
        "total": round(sum(deduction_split.values()), 4),
        **{bucket: round(deduction_split[bucket], 4) for bucket in REACHABILITY_BUCKETS},
    }
    ordered["deduction_by_biomarker"] = {k: round(v, 4) for k, v in sorted(deduction_by_biomarker.items())}
    ordered["dry_violations"] = dry_violation_records(findings)
    ordered["coverage"] = coverage.get(ordered["path"], {})
    return ordered


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    below_floor = [r for r in records if r["score"] is not None and r["score"] < BELOW_FLOOR_THRESHOLD]

    def sum_defect_split(rows: list[dict[str, Any]]) -> dict[str, int]:
        total = {"structural": 0, "historical": 0, "other_defect": 0}
        for r in rows:
            for k, v in r["defect_split"].items():
                total[k] += v
        return total

    def sum_biomarker(rows: list[dict[str, Any]], dimension: str) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for r in rows:
            for biomarker, count in r["findings_by_biomarker"].items():
                if biomarker in PERFORMANCE_BIOMARKERS and dimension == "performance":
                    totals[biomarker] += count
        return dict(sorted(totals.items()))

    def sum_deduction(rows: list[dict[str, Any]]) -> dict[str, float]:
        total = dict.fromkeys(REACHABILITY_BUCKETS, 0.0)
        for r in rows:
            for bucket in REACHABILITY_BUCKETS:
                total[bucket] += r["deduction"][bucket]
        rounded = {bucket: round(total[bucket], 4) for bucket in REACHABILITY_BUCKETS}
        rounded["total"] = round(sum(total.values()), 4)
        return rounded

    def sum_deduction_by_biomarker(rows: list[dict[str, Any]]) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for r in rows:
            for biomarker, impact in r["deduction_by_biomarker"].items():
                totals[biomarker] += impact
        return {k: round(v, 4) for k, v in sorted(totals.items())}

    defect_split_all = sum_defect_split(records)
    defect_split_below_floor = sum_defect_split(below_floor)
    deduction_all = sum_deduction(records)
    unclassified_types = sorted({b for r in records for b in r["deduction_by_biomarker"] if classify_reachability(b) == "unclassified"})
    dry_total = sum(len(r["dry_violations"]) for r in records)

    return {
        "src_file_count": len(records),
        "files_below_floor": len(below_floor),
        "floor_threshold": BELOW_FLOOR_THRESHOLD,
        "defect_findings_total": sum(defect_split_all.values()),
        "defect_split_total": defect_split_all,
        "defect_findings_below_floor": sum(defect_split_below_floor.values()),
        "defect_split_below_floor": defect_split_below_floor,
        "performance_findings_by_biomarker": sum_biomarker(records, "performance"),
        "performance_findings_total": sum(sum_biomarker(records, "performance").values()),
        # phaze-bk9el.1: deduction, repo-wide. `reachable_pct_of_total` is the number the epic's
        # ledger has to lead with -- the share of the deduction any amount of refactoring could move.
        "deduction_total": deduction_all,
        "deduction_total_below_floor": sum_deduction(below_floor),
        "deduction_by_biomarker": sum_deduction_by_biomarker(records),
        "reachable_pct_of_total": round(deduction_all["reachable"] / deduction_all["total"] * 100.0, 2) if deduction_all["total"] else None,
        "history_derived_pct_of_total": round(deduction_all["history_derived"] / deduction_all["total"] * 100.0, 2)
        if deduction_all["total"]
        else None,
        "unclassified_biomarker_types": unclassified_types,
        "dry_violation_count": dry_total,
    }


def load_coverage_json(path: Path) -> dict[str, dict[str, Any]]:
    """path -> {statement/branch coverage} from a ``coverage json`` report, plus a ``__totals__`` row.

    coverage.py is the source of truth here, not the index: repowise ingests LINE coverage from
    Cobertura and leaves ``branch_coverage_pct`` null on this repo, and the per-bead branch gate
    (``scripts/branch_coverage_check.py``) compares against these same fields. Taking both from one
    report is what keeps the two artifacts consistent with each other.

    ``percent_branches_covered`` is None for a file with no branches — that file is not 0% and not
    100% branch-covered, it is unmeasurable, and recording either number would make it look like it
    moved when its first branch appears.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}

    def row(summary: dict[str, Any]) -> dict[str, Any]:
        num_branches = summary.get("num_branches") or 0
        return {
            "covered_branches": summary.get("covered_branches", 0),
            "covered_lines": summary.get("covered_lines", 0),
            "num_branches": num_branches,
            "num_statements": summary.get("num_statements", 0),
            "percent_branches_covered": summary.get("percent_branches_covered") if num_branches else None,
            "percent_covered_combined": summary.get("percent_covered"),
            "percent_statements_covered": summary.get("percent_statements_covered"),
        }

    for file_path, info in (data.get("files") or {}).items():
        summary = info.get("summary") or {}
        # coverage.py reports paths relative to the rootdir (relative_files = true), and the index
        # keys on repo-relative paths -- but the report omits the `src/` prefix for an installed
        # package layout in some invocations, so accept both spellings.
        out[file_path] = row(summary)
        if not file_path.startswith("src/"):
            out[f"src/{file_path}"] = row(summary)
    totals = data.get("totals")
    if isinstance(totals, dict):
        out["__totals__"] = row(totals)
    return out


def write_json(payload: dict[str, Any], out: Any) -> None:
    # sort_keys=True matches the repo's pretty-format-json pre-commit hook (--autofix --indent=2,
    # which sorts keys by default), so a committed baseline is byte-identical to a fresh run.
    json.dump(payload, out, indent=2, sort_keys=True)
    out.write("\n")


def write_csv(payload: dict[str, Any], out: Any) -> None:
    fieldnames = [
        "path",
        "score",
        "defect_score",
        "maintainability_score",
        "performance_score",
        "line_coverage_pct",
        "branch_coverage_pct",
        "max_ccn",
        "max_nesting",
        "nloc",
        "duplication_pct",
        "cov_percent_statements",
        "cov_percent_branches",
        "deduction_total",
        "deduction_reachable",
        "deduction_history_derived",
        "deduction_performance",
        "deduction_unclassified",
        "dry_violation_count",
        "total_findings",
        "defect_structural",
        "defect_historical",
        "defect_other_defect",
        "dim_defect",
        "dim_maintainability",
        "dim_performance",
        "findings_by_biomarker_json",
    ]
    writer = csv.DictWriter(out, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for record in payload["files"]:
        dims = record["findings_by_dimension"]
        split = record["defect_split"]
        ded = record["deduction"]
        cov = record["coverage"]
        writer.writerow(
            {
                "path": record["path"],
                "score": record["score"],
                "defect_score": record["defect_score"],
                "maintainability_score": record["maintainability_score"],
                "performance_score": record["performance_score"],
                "line_coverage_pct": record["line_coverage_pct"],
                "branch_coverage_pct": record["branch_coverage_pct"],
                "max_ccn": record["max_ccn"],
                "max_nesting": record["max_nesting"],
                "nloc": record["nloc"],
                "duplication_pct": record["duplication_pct"],
                "cov_percent_statements": cov.get("percent_statements_covered"),
                "cov_percent_branches": cov.get("percent_branches_covered"),
                "deduction_total": ded["total"],
                "deduction_reachable": ded["reachable"],
                "deduction_history_derived": ded["history_derived"],
                "deduction_performance": ded["performance"],
                "deduction_unclassified": ded["unclassified"],
                "dry_violation_count": len(record["dry_violations"]),
                "total_findings": record["total_findings"],
                "defect_structural": split["structural"],
                "defect_historical": split["historical"],
                "defect_other_defect": split["other_defect"],
                "dim_defect": dims.get("defect", 0),
                "dim_maintainability": dims.get("maintainability", 0),
                "dim_performance": dims.get("performance", 0),
                "findings_by_biomarker_json": json.dumps(record["findings_by_biomarker"], sort_keys=True),
            }
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="path to the Repowise sqlite index (default: %(default)s)")
    parser.add_argument("--repo", default="phaze", help="repository name in the index (default: %(default)s)")
    parser.add_argument("--format", choices=("json", "csv"), default="json", help="output format (default: %(default)s)")
    parser.add_argument(
        "--baseline-commit",
        default=None,
        help=(
            "the commit this baseline describes, recorded as `baseline_commit` (phaze-bk9el.1 criterion 6). "
            "`analyzed_commit` comes from repositories.head_commit, which `repowise update` is known NOT to "
            "refresh, so it can name a commit days older than the index -- pass this to record the real one."
        ),
    )
    parser.add_argument(
        "--index-commit",
        default=None,
        help="the commit the Repowise index was last synced to, recorded as `index_commit` (see --baseline-commit)",
    )
    parser.add_argument(
        "--coverage-json",
        type=Path,
        default=None,
        help="a `coverage json` report to merge authoritative statement/branch coverage from (phaze-bk9el.1)",
    )
    parser.add_argument("--out", type=Path, default=None, help="output file path (default: stdout)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if not args.db.exists():
        sys.stderr.write(
            f"error: no Repowise index at {args.db} -- run `repowise init` in that checkout, "
            "or pass --db to point at a checkout that already has one\n"
        )
        return 1

    coverage: dict[str, dict[str, Any]] = {}
    if args.coverage_json is not None:
        if not args.coverage_json.exists():
            sys.stderr.write(f"error: no coverage report at {args.coverage_json}\n")
            return 1
        coverage = load_coverage_json(args.coverage_json)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        repo_id, head_commit = resolve_repository(conn, args.repo)
        metrics = load_metrics(conn, repo_id)
        findings = load_findings(conn, repo_id)
    finally:
        conn.close()

    records = [build_file_record(metric, findings.get(metric["file_path"], []), coverage) for metric in metrics]
    summary = build_summary(records)

    payload = {
        "repository": args.repo,
        "analyzed_commit": head_commit,
        "baseline_commit": args.baseline_commit,
        "index_commit": args.index_commit,
        "biomarker_classification": {
            "structural": sorted(STRUCTURAL_BIOMARKERS),
            "historical": sorted(HISTORICAL_BIOMARKERS),
            "performance": sorted(PERFORMANCE_BIOMARKERS),
            "reachable": sorted(REACHABLE_BIOMARKERS),
            "history_derived": sorted(HISTORY_DERIVED_BIOMARKERS),
        },
        "coverage_source": str(args.coverage_json) if args.coverage_json else None,
        "coverage_totals": coverage.get("__totals__", {}),
        "summary": summary,
        "files": records,
    }

    out = args.out.open("w") if args.out else sys.stdout
    try:
        if args.format == "json":
            write_json(payload, out)
        else:
            write_csv(payload, out)
    finally:
        if args.out:
            out.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
