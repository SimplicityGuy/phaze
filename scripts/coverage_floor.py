"""Enforce phaze's two LINE-coverage floors: repo-wide 95% and per-module 90% (COV-01, D-01/D-02/D-03).

Reads ``coverage json`` output (``coverage.json`` in the current working directory) and enforces:

* the **repo-wide** floor -- 95% line coverage over the combined report (``TOTAL_FLOOR``); and
* a single uniform **per-module** floor (``FLOOR``, D-04, raised 85 -> 90 once every module cleared
  90%) over every tracked source file.

Runs inside ``just test-cov`` and ``just coverage-combine``; in the latter it runs AFTER
``coverage combine`` + ``coverage json``, so it sees the authoritative COMBINED coverage
(Phase 63 D-02) -- never a partial per-bucket shard.

WHY THE REPO-WIDE FLOOR MOVED HERE (phaze-bk9el.21). It used to be ``fail_under = 95`` in
``[tool.coverage.report]``, enforced by pytest-cov and by ``coverage report --fail-under=95``.
Enabling ``branch = true`` repo-wide changed what that knob MEASURES: with branches on,
coverage.py's total is the COMBINED figure ``(covered_lines + covered_branches) /
(num_statements + num_branches)``, and coverage.py offers no option to make ``fail_under``
line-only (verified against coverage 7.15.4). Leaving the floors on that knob would have
quietly converted both of phaze's repo-wide gates into branch-sensitive gates -- the exact thing
the operator's decision on phaze-bk9el.21 says not to do ("the repo-wide gate stays on lines at
95%"), and the exact shape ADR-0012 rule 3 warns about: a gate that keeps its number while
silently changing the quantity it measures.

So both floors are read here from ``percent_statements_covered``, which coverage.py reports
alongside ``percent_branches_covered`` and ``percent_covered`` (combined). The metric is named in
every line this script prints, so a failure can never be misread as the other one.

Branch coverage is MEASURED and REPORTED -- see ``just branch-check``, which gates it per bead on
the files that bead touched -- but it is deliberately not gated repo-wide.

Inputs:
    ``coverage.json`` (cwd) -- the combined ``coverage json`` report. Its top-level ``files`` dict is
    the self-maintaining tracked set (D-03): each key is a source path, each value a ``summary`` with
    ``num_statements`` and ``percent_statements_covered``. ``totals`` carries the same keys for the
    whole report.

Output:
    A printed per-module report. On failure, the offending ``path`` + percentage for every sub-floor
    module and/or the repo-wide total; on success, a single all-clear line naming both floors.

Exit semantics (T-64-01, FAIL CLOSED):
    0  -- the repo-wide total is >= TOTAL_FLOOR and every tracked module is >= FLOOR (or exempt /
          zero-statement).
    1  -- the repo-wide total is below TOTAL_FLOOR, OR at least one tracked module is below FLOOR,
          OR the report carries NO tracked files (an empty ``files`` dict -- a report with zero
          modules is treated as a failed measurement, never an all-clear).
    !=0 -- a missing / empty-string / unparseable / ``files``-less ``coverage.json`` raises, which
           propagates as a non-zero exit. A missing gate input NEVER exits 0.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


FLOOR = 90.0
# The repo-wide floor, on LINES. Previously `fail_under = 95` in pyproject's
# [tool.coverage.report]; see this module's docstring for why enabling branch coverage forced it
# to move rather than letting it silently become a combined-metric gate.
TOTAL_FLOOR = 95.0
# D-09 exemptions: {relative_path: "written justification"}. Keep empty unless a module is genuinely
# untestable AND D-08 seams cannot help. Each entry MUST carry a written justification and a reviewer
# must confirm it. Given D-08 seams are allowed, this is expected to stay empty.
EXEMPT: dict[str, str] = {}


def line_percent(summary: dict[str, float]) -> float:
    """Return the LINE coverage percentage for one coverage.json ``summary`` block.

    ``percent_covered`` is the combined line+branch figure once ``branch = true`` is set, so it is
    deliberately not what this gate reads. ``percent_statements_covered`` is present on every
    coverage.py >= 7.6 report, branch mode or not; the division is the documented fallback for an
    older report and yields the identical number.
    """
    if "percent_statements_covered" in summary:
        return float(summary["percent_statements_covered"])
    statements = float(summary["num_statements"])
    return 100.0 if statements == 0 else float(summary["covered_lines"]) / statements * 100.0


def main() -> int:
    data = json.loads(Path("coverage.json").read_text(encoding="utf-8"))
    files = data["files"]
    if not files:  # FAIL CLOSED (T-64-01): a report with zero tracked modules is a broken
        # measurement (e.g. no shards combined), not an all-clear — an empty loop must never exit 0.
        print("❌ coverage.json has no tracked files — refusing to pass an empty coverage report.")  # noqa: T201
        return 1
    if "totals" not in data:
        # FAIL CLOSED: the repo-wide floor is read from `totals`; a report without one is a broken
        # measurement, and skipping the total silently would leave only the per-module floor armed.
        print("❌ coverage.json has no 'totals' section — refusing to pass an incomplete report.")  # noqa: T201
        return 1
    failed = False
    total_pct = line_percent(data["totals"])
    if total_pct < TOTAL_FLOOR:
        print(f"❌ Repo-wide LINE coverage {total_pct:.2f}% is below the {TOTAL_FLOOR:.0f}% floor.")  # noqa: T201
        failed = True
    failures: list[tuple[str, float]] = []
    for path, info in sorted(files.items()):
        if path in EXEMPT:
            continue
        if info["summary"]["num_statements"] == 0:  # __init__.py / empty modules
            continue
        pct = line_percent(info["summary"])
        if pct < FLOOR:
            failures.append((path, pct))
    if failures:
        print(f"❌ Per-module LINE coverage floor {FLOOR:.0f}% not met:")  # noqa: T201
        for path, pct in failures:
            print(f"   {pct:6.2f}%  {path}")  # noqa: T201
        failed = True
    if failed:
        return 1
    branch_pct = data["totals"].get("percent_branches_covered")
    branch_note = (
        "" if branch_pct is None else f"  (branch coverage {float(branch_pct):.2f}%, measured but not gated repo-wide — see `just branch-check`)"
    )
    print(  # noqa: T201
        f"✅ Repo-wide LINE coverage {total_pct:.2f}% ≥ {TOTAL_FLOOR:.0f}%; all tracked modules ≥ {FLOOR:.0f}% lines.{branch_note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
