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

METRIC_COLUMNS = (
    "file_path",
    "score",
    "defect_score",
    "maintainability_score",
    "performance_score",
    "line_coverage_pct",
)

BELOW_FLOOR_THRESHOLD = 6.0


def classify_defect_biomarker(biomarker_type: str) -> str:
    """Bucket one defect-dimension biomarker into structural / historical / other_defect."""
    if biomarker_type in STRUCTURAL_BIOMARKERS:
        return "structural"
    if biomarker_type in HISTORICAL_BIOMARKERS:
        return "historical"
    return "other_defect"


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
        """
        SELECT file_path, score, defect_score, maintainability_score, performance_score, line_coverage_pct
        FROM health_file_metrics
        WHERE repository_id = ? AND file_path LIKE 'src/%'
        ORDER BY file_path
        """,
        (repo_id,),
    ).fetchall()
    return [dict(zip(METRIC_COLUMNS, row, strict=True)) for row in rows]


def load_findings(conn: sqlite3.Connection, repo_id: str) -> dict[str, list[tuple[str, str]]]:
    """file_path -> list of (biomarker_type, dimension) for every open src/ finding."""
    rows = conn.execute(
        """
        SELECT file_path, biomarker_type, dimension
        FROM health_findings
        WHERE repository_id = ? AND file_path LIKE 'src/%' AND status = 'open'
        """,
        (repo_id,),
    ).fetchall()
    findings: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for file_path, biomarker_type, dimension in rows:
        findings[file_path].append((biomarker_type, dimension or "unknown"))
    return findings


def build_file_record(metric: dict[str, Any], findings: list[tuple[str, str]]) -> dict[str, Any]:
    by_biomarker: dict[str, int] = defaultdict(int)
    by_dimension: dict[str, int] = defaultdict(int)
    defect_split = {"structural": 0, "historical": 0, "other_defect": 0}
    for biomarker_type, dimension in findings:
        by_biomarker[biomarker_type] += 1
        by_dimension[dimension] += 1
        if dimension == "defect":
            defect_split[classify_defect_biomarker(biomarker_type)] += 1

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

    defect_split_all = sum_defect_split(records)
    defect_split_below_floor = sum_defect_split(below_floor)

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
    }


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
        writer.writerow(
            {
                "path": record["path"],
                "score": record["score"],
                "defect_score": record["defect_score"],
                "maintainability_score": record["maintainability_score"],
                "performance_score": record["performance_score"],
                "line_coverage_pct": record["line_coverage_pct"],
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

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        repo_id, head_commit = resolve_repository(conn, args.repo)
        metrics = load_metrics(conn, repo_id)
        findings = load_findings(conn, repo_id)
    finally:
        conn.close()

    records = [build_file_record(metric, findings.get(metric["file_path"], [])) for metric in metrics]
    summary = build_summary(records)

    payload = {
        "repository": args.repo,
        "analyzed_commit": head_commit,
        "biomarker_classification": {
            "structural": sorted(STRUCTURAL_BIOMARKERS),
            "historical": sorted(HISTORICAL_BIOMARKERS),
            "performance": sorted(PERFORMANCE_BIOMARKERS),
        },
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
