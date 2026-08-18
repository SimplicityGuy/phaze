"""Fail the repowise coverage refresh unless BOTH ingests landed, at the SAME commit as the index.

``scripts/repowise-coverage.sh`` (driven by ``just repowise-coverage``) runs the five-step refresh:
reindex, suite-with-contexts, ``repowise coverage add .coverage``, ``coverage xml`` +
``repowise coverage add coverage.xml``, ``repowise health``. This module is the gate it runs at the
end, and it exists because three of that path's failure modes are SILENT.

Trap 1 -- the ``.coverage``-only partial success (phaze-2rgq2).
    ``repowise coverage add .coverage`` prints ``Built the test-to-code map: N test->file
    record(s)`` and ``repowise coverage status`` then renders a populated map. It looks finished.
    It is not: that report carries per-test CONTEXTS but no per-file line totals, so
    ``coverage.line_coverage_pct`` stays ``None`` and the ``untested_hotspot`` biomarker keeps
    firing on files that are 96-100% covered. Only the Cobertura ``coverage.xml`` populates the
    per-file table. A refresh that exits 0 having done only the first ingest leaves every health
    score wrong while every surface reports success -- so this gate requires BOTH blocks and
    treats a null ``line_coverage_pct`` as a hard failure, not a missing optional field.

Trap 2 -- index/coverage commit skew (phaze-2rgq2).
    Health findings carry LINE NUMBERS. An index built at commit A paired with coverage measured
    at commit B maps coverage onto lines that have moved. The suite takes ~21 minutes, which is
    exactly long enough for ``main`` to advance underneath the run; during the session that filed
    this bead the index went behind twice within the hour. repowise records an
    ``ingested_commit_sha`` on each ingest and a ``state.last_sync_commit`` on the index, so the
    pairing is machine-checkable: all three must equal the HEAD the run started from.

Trap 3 -- the fragment ingest (found while verifying this recipe, phaze-2rgq2).
    ``repowise coverage add`` maps the report's paths onto the files it has INDEXED, keeps whatever
    maps, prints ``N report file(s) did not map to the repo tree`` as a note, and exits 0. A run
    that ingested 1 of 249 files therefore reports a real ``line_coverage_pct`` and passes every
    emptiness check. The report was produced from the same tree seconds earlier, so a shortfall is
    a structural mismatch -- hence ``--coverage-xml``, which makes the gate compare the ingested
    file count against the report's own.

Inputs (paths, so the caller keeps the raw JSON for debugging):
    ``--coverage-status``  ``repowise coverage status --format json`` output.
    ``--index-status``     ``repowise status --format json`` output.
    ``--expected-commit``  the ``git rev-parse HEAD`` captured BEFORE the run started.
    ``--coverage-xml``     the report that was ingested; every file in it must have mapped.

Exit semantics (FAIL CLOSED):
    0   -- both ingests present, ``line_coverage_pct`` is a real number, and index + both ingests
           are pinned to ``--expected-commit``.
    1   -- any of the above is false; every violated condition is named on stderr.
    !=0 -- a missing/unparseable status file raises, which propagates. A missing gate input NEVER
           exits 0: "no evidence" is not "verified".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


# Shortest prefix we will accept when comparing two git shas. repowise records the full 40-char
# sha today; tolerating a prefix means a future short-sha does not turn this gate into a false red,
# while 7 chars keeps an accidental empty/one-char value from matching everything.
_MIN_SHA_PREFIX = 7


def _load_json(path: Path) -> dict[str, Any]:
    """Parse a repowise ``--format json`` dump, tolerating human preamble lines before the object.

    ``repowise health --format json`` prints a ``repowise health -- <repo>`` banner ahead of the
    JSON, so no repowise JSON surface can be assumed to start at byte 0. Slice from the first
    ``{`` rather than depending on which subcommand happens to be clean today.
    """
    raw = path.read_text(encoding="utf-8")
    start = raw.find("{")
    if start < 0:
        msg = f"{path} contains no JSON object"
        raise ValueError(msg)
    parsed = json.loads(raw[start:])
    if not isinstance(parsed, dict):
        msg = f"{path} does not contain a JSON object at the top level"
        raise TypeError(msg)
    return parsed


def _block(status: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``status[key]`` as a mapping — an absent or null block reads as empty, not as an error.

    repowise renders a never-ingested block as ``null`` and a partially-ingested one as a dict with
    zeroed fields; both mean "not usable", so collapse them to the same empty shape here and let the
    per-field checks below produce the specific message.
    """
    value = status.get(key)
    return value if isinstance(value, dict) else {}


def _shas_match(left: str, right: str) -> bool:
    """True when two git shas name the same commit, allowing either to be an abbreviation."""
    lhs, rhs = left.strip().lower(), right.strip().lower()
    if len(lhs) < _MIN_SHA_PREFIX or len(rhs) < _MIN_SHA_PREFIX:
        return False
    return lhs.startswith(rhs) or rhs.startswith(lhs)


def count_report_files(coverage_xml: Path) -> int:
    """Count the distinct source files a Cobertura report covers.

    Read with a regex rather than an XML parser on purpose: the file is locally generated by
    ``coverage xml`` seconds earlier, the shape is fixed (``<class ... filename="...">``), and an
    XML parser here would be a bandit finding for no benefit.
    """
    text = coverage_xml.read_text(encoding="utf-8")
    return len(set(re.findall(r'<class\b[^>]*\bfilename="([^"]*)"', text)))


def check(
    coverage_status: dict[str, Any],
    index_status: dict[str, Any],
    expected_commit: str,
    report_file_count: int | None = None,
) -> list[str]:
    """Return every reason this refresh is not trustworthy; an empty list means it is."""
    failures: list[str] = []

    if not index_status.get("indexed"):
        failures.append("repowise reports this repo as NOT indexed — run `repowise init` in the durable checkout first.")
    index_sha = str(_block(index_status, "state").get("last_sync_commit") or "")
    if not _shas_match(index_sha, expected_commit):
        failures.append(f"index is at commit {index_sha or '<none>'}, not {expected_commit} — the index and the coverage describe different trees.")

    coverage = _block(coverage_status, "coverage")
    file_count = coverage.get("file_count") or 0
    line_pct = coverage.get("line_coverage_pct")
    if not coverage or file_count == 0:
        failures.append(
            "per-file line coverage is EMPTY — `coverage xml` + `repowise coverage add coverage.xml` did not land (this is trap 1: the test-to-code map alone looks like success)."
        )
    elif not isinstance(line_pct, (int, float)):
        failures.append(
            f"per-file line coverage has {file_count} file(s) but line_coverage_pct is {line_pct!r} — the Cobertura ingest did not populate the per-file table."
        )
    elif report_file_count is not None and file_count < report_file_count:
        # Trap 3, found the hard way while verifying this recipe (phaze-2rgq2). `repowise coverage
        # add` maps report paths onto the files it has INDEXED and silently keeps whatever maps:
        # it prints "N report file(s) did not map to the repo tree" as a note and still exits 0.
        # A run that ingested 1 of 249 files therefore reports a real line_coverage_pct, passes
        # every emptiness check above, and is completely wrong. The report was generated from this
        # same tree seconds earlier, so anything short of full mapping is a structural mismatch
        # (index registered under a different path, stale index, moved files) — not a rounding
        # detail. Only a SHORTFALL fails: repowise upserts rather than replaces, so rows left over
        # from an earlier report legitimately make the stored count exceed this one.
        failures.append(
            f"only {file_count} of the {report_file_count} file(s) in coverage.xml mapped into the index — "
            "repowise reports the rest as 'did not map to the repo tree' and still exits 0. The ingested coverage is a fragment, not the run."
        )

    test_map = _block(coverage_status, "test_map")
    if not test_map or (test_map.get("pair_count") or 0) == 0:
        failures.append(
            "the test-to-code map is EMPTY — the suite must run with `--cov-context=test`, otherwise `get_risk`'s tests_to_run and the impacted-tests skill return nothing."
        )

    for label, block in (("per-file coverage", coverage), ("test-to-code map", test_map)):
        if not block:
            continue  # already reported as empty above; a sha check on nothing adds noise.
        sha = str(block.get("ingested_commit_sha") or "")
        if not _shas_match(sha, expected_commit):
            failures.append(
                f"{label} was ingested at commit {sha or '<none>'}, not {expected_commit} — findings carry line numbers, so this maps coverage onto stale lines."
            )

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a repowise coverage refresh landed both ingests at the indexed commit.")
    parser.add_argument("--coverage-status", required=True, type=Path, help="`repowise coverage status --format json` output")
    parser.add_argument("--index-status", required=True, type=Path, help="`repowise status --format json` output")
    parser.add_argument("--expected-commit", required=True, help="git rev-parse HEAD captured before the run started")
    parser.add_argument("--coverage-xml", type=Path, help="the coverage.xml that was ingested; its file count must all have mapped into the index")
    args = parser.parse_args(argv)

    coverage_status = _load_json(args.coverage_status)
    index_status = _load_json(args.index_status)
    report_file_count = count_report_files(args.coverage_xml) if args.coverage_xml else None
    failures = check(coverage_status, index_status, args.expected_commit, report_file_count)

    if failures:
        print("❌ repowise coverage refresh is NOT trustworthy:", file=sys.stderr)  # noqa: T201
        for failure in failures:
            print(f"   - {failure}", file=sys.stderr)  # noqa: T201
        return 1

    coverage = _block(coverage_status, "coverage")
    test_map = _block(coverage_status, "test_map")
    print(  # noqa: T201
        f"✅ repowise coverage refresh verified at {args.expected_commit[:12]}: "
        f"{coverage.get('file_count')} file(s) at {coverage.get('line_coverage_pct')}% line coverage, "
        f"{test_map.get('pair_count')} test->file pair(s) from {test_map.get('test_count')} test(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
