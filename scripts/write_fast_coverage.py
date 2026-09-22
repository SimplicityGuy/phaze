"""Write a clearly scoped branch report after the selected fast test run.

The selected run is never measured against the repo-wide coverage floor. Keeping its data and
report separate also preserves any full-suite coverage.json already present in this worktree.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess  # nosec B404 - fixed git argv, no shell

import coverage


DATA = Path(".coverage.fast")
REPORT = Path(".fast-coverage.json")


def main() -> int:
    if not DATA.is_file():
        raise SystemExit("selected test run produced no .coverage.fast data")
    head = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()  # noqa: S607  # nosec B603
    cov = coverage.Coverage(data_file=str(DATA))
    cov.load()
    cov.set_option("report:fail_under", 0)
    cov.json_report(outfile=str(REPORT))
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    if "num_branches" not in report.get("totals", {}):
        raise SystemExit("selected test run produced no branch data")
    report["_phaze_scope"] = {"kind": "selected-tests", "head": head}
    REPORT.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
