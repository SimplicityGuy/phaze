"""Fail if any tracked phaze module is below the per-module coverage floor (COV-01, D-01/D-02/D-03).

Reads ``coverage json`` output (``coverage.json`` in the current working directory) and enforces a
single uniform floor (D-04, raised 85 -> 90 once every module cleared 90%) over every tracked source file. Runs inside ``just coverage-combine``
AFTER ``coverage combine`` + ``coverage json``, so it sees the authoritative COMBINED coverage
(Phase 63 D-02) -- never a partial per-bucket shard.

Inputs:
    ``coverage.json`` (cwd) -- the combined ``coverage json`` report. Its top-level ``files`` dict is
    the self-maintaining tracked set (D-03): each key is a source path, each value a ``summary`` with
    ``num_statements`` and the raw float ``percent_covered``.

Output:
    A printed per-module report. On failure, the offending ``path`` + percentage for every sub-floor
    module; on success, a single all-clear line.

Exit semantics (T-64-01, FAIL CLOSED):
    0  -- every tracked module is >= FLOOR (or exempt / zero-statement).
    1  -- at least one tracked module is below FLOOR, OR the report carries NO tracked files
          (an empty ``files`` dict -- a report with zero modules is treated as a failed
          measurement, never an all-clear).
    !=0 -- a missing / empty-string / unparseable / ``files``-less ``coverage.json`` raises, which
           propagates as a non-zero exit. A missing gate input NEVER exits 0.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


FLOOR = 90.0
# D-09 exemptions: {relative_path: "written justification"}. Keep empty unless a module is genuinely
# untestable AND D-08 seams cannot help. Each entry MUST carry a written justification and a reviewer
# must confirm it. Given D-08 seams are allowed, this is expected to stay empty.
EXEMPT: dict[str, str] = {}


def main() -> int:
    data = json.loads(Path("coverage.json").read_text(encoding="utf-8"))
    files = data["files"]
    if not files:  # FAIL CLOSED (T-64-01): a report with zero tracked modules is a broken
        # measurement (e.g. no shards combined), not an all-clear — an empty loop must never exit 0.
        print("❌ coverage.json has no tracked files — refusing to pass an empty coverage report.")  # noqa: T201
        return 1
    failures: list[tuple[str, float]] = []
    for path, info in sorted(files.items()):
        if path in EXEMPT:
            continue
        if info["summary"]["num_statements"] == 0:  # __init__.py / empty modules
            continue
        pct = info["summary"]["percent_covered"]
        if pct < FLOOR:
            failures.append((path, pct))
    if failures:
        print(f"❌ Per-module coverage floor {FLOOR:.0f}% not met:")  # noqa: T201
        for path, pct in failures:
            print(f"   {pct:6.2f}%  {path}")  # noqa: T201
        return 1
    print(f"✅ All tracked modules ≥ {FLOOR:.0f}% (combined coverage).")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
