"""Stamp `_phaze_scope` onto a full-suite coverage.json so `just branch-check` can tell it is
safe to reuse without rerunning tests (phaze-vad6y).

CLAUDE.md documented `just branch-check` as "free after any `check`" -- reading whatever
coverage.json a prior gate run left behind. That was aspirational, not measured: nothing ever
recorded WHICH tree a full-suite `coverage.json` was produced against, so
`scripts/branch_coverage_check.py` had no way to tell a same-tree report from a stale one, and
`just branch-check`'s recipe never even tried to reuse one -- it unconditionally re-ran
`_test-branch-scoped` on EVERY invocation, whatever the diff: a cheaper selected-test subset when
the change mapped cleanly, a full-suite escalation otherwise. Every invocation the 2026-09-22/23
dispatch actually recorded escalated, costing a genuine ~20-24 minutes each time -- the escalation
branch is what real beads hit, not a worst case invented for this docstring.

This mirrors `scripts/write_fast_coverage.py`'s existing `_phaze_scope` stamp on the SCOPED
report (`.fast-coverage.json`, `kind: "selected-tests"`), generalised to the FULL report
(`coverage.json`, `kind: "full"`). It is wired into the two recipes that produce a full-suite
`coverage.json` from an ACTUAL pytest invocation: `test-cov` (the caller-owned-triplet and
disabled-parallel serial paths) and `scripts/parallel_test_runner.py`'s `_postprocess` (the
DEFAULT two-worker path -- it writes `coverage.json` itself via `coverage json` and does not go
through `test-cov`, which is why it needs its own record/stamp pair rather than inheriting
`test-cov`'s). `coverage-combine` (CI's per-shard combine) deliberately does NOT stamp: its shards
are measured at different times against a tree that has already moved on by the time `combine`
runs, so a bracket placed around `combine` itself would describe nothing real -- see "WHY NOT
`coverage-combine`" below. `find_reusable_report` in `scripts/branch_coverage_check.py` is the
consumer.
`tests/shared/test_validation_gate_recipes.py::test_every_full_coverage_writer_reachable_from_test_validate_stamps_its_output`
pins both real producers, and scans for an unlisted THIRD one, so a future writer cannot silently
reintroduce the gap this bead closed.

WHY NOT `coverage-combine`. Each CI shard (`test-bucket`) runs pytest separately, potentially on a
different runner, at a different moment, all against the SAME commit but with no promise the tree
was clean or unchanging during any one shard's own run. `coverage-combine` then runs once, AFTER
every shard has already finished, merely combining their already-written `.coverage.*` data files
-- it never itself runs pytest, so a bracket around the `combine`/`json` calls would capture "was
the tree clean when `combine` started", a fact with no relationship to what any shard actually
measured. Earlier drafts of this fix bracketed `coverage-combine` anyway; that was wrong and has
been removed. CI does not upload `coverage.json` as an artifact either, so nothing downstream ever
reads a stamp there. If CI coverage-combine output is ever fed to `branch-check`, that is a new
requirement needing its own design, not an extension of this bracket.

TWO-PHASE, NOT ONE-SHOT, AND WHY. Reading HEAD only at the END of the run (the original design)
is wrong: a full-suite run takes minutes, and by the time this script runs, HEAD or the working
tree's cleanliness may no longer describe what pytest actually measured.

  * A commit can land DURING the run (the developer commits their WIP partway through a long
    `just check`). Reading HEAD only at the end would stamp the report as measuring the NEW
    commit's tree, when it actually measured the OLD tree plus whatever was staged/unstaged
    before that commit -- usually the same bytes, but not provably so, and not always so (a
    `git pull`/rebase landing mid-run changes file content out from under an in-flight read).
  * A dirty tree can be made to look clean DURING the run (`git stash` after pytest has already
    read the files). Checking dirtiness only at the end would then see a clean tree and a
    stamped HEAD, and `find_reusable_report` would treat the result as a trustworthy measurement
    of that HEAD's committed tree -- when it actually measured HEAD's tree PLUS a diff that no
    longer even exists on disk.

So this script runs in two phases, bracketing the coverage-producing step:

    token="$(uv run python scripts/stamp_coverage_scope.py --record-start)"  # BEFORE pytest
    uv run pytest --cov ...                                                  # the run itself
    uv run python scripts/stamp_coverage_scope.py --expect-token "$token"    # AFTER

`--record-start` captures HEAD and working-tree cleanliness at the moment just before the
coverage-producing step begins -- the instant closest to what it will actually read -- into a
one-shot marker file (`--start-marker`, default `.coverage-scope-start.json`, git-ignored), and
prints a freshly minted, per-call TOKEN to stdout (the only thing `--record-start` ever prints to
stdout, so a caller can capture it directly via `$(...)`).

WHY A TOKEN, NOT JUST THE MARKER FILE. The marker is ONE file shared by whichever run happens to
be using it, and two runs can genuinely overlap on one host: a full run starts on a dirty tree
(recording `dirty: true`) and, before it finishes, the operator `git stash`es and starts a SECOND
run, which overwrites the SAME marker with `dirty: false`. If the session lock then refuses the
second run before it collects (the ordinary outcome -- see CLAUDE.md's "one database, one pytest
process"), it never reaches its own stamp call, but the FIRST run finishes, reads the marker --
now the second run's clean record -- and would stamp its own (actually dirty) measurement as
trustworthy. A per-run token closes this: each `--record-start` mints its own, the CALLER threads
it through its own local variable (never through the shared marker file except as the value
written into it), and the stamp step refuses unless the marker's token matches the EXACT token its
own `--record-start` produced -- so a marker clobbered by a second run is detected as "not mine"
rather than trusted.

The stamp step refuses to stamp, and always consumes (deletes) the marker so a stale one can never
leak into an unrelated later attempt, when:

  * no marker exists, or its token does not match `--expect-token` (no `--record-start` ran first,
    a `just test-cov` invoked directly without going through this bracket, or another run's
    `--record-start` clobbered this run's marker -- all three degrade safely to "unstamped", never
    to a false stamp);
  * the tree was ALREADY dirty when the run started -- a dirty measurement has no exact,
    git-addressable tree to name, so there is nothing correct to stamp it with, committed or not;
  * HEAD at the end differs from HEAD at the start -- something landed while the run was in
    flight, so the tree the report describes and the tree HEAD now names may not be the same
    bytes.

`record_start` itself ALWAYS clears any existing marker FIRST, before doing anything else --
including before it can fail on an unresolvable HEAD -- so a failed or skipped `--record-start`
never leaves a STALE marker (from an earlier, unrelated, possibly aborted run) sitting around for
a later stamp call to misread as describing THIS run. A caller that cannot capture
`--record-start`'s token (a nonzero exit, or empty stdout) must skip the later stamp call
entirely rather than calling it with no `--expect-token` -- see `scripts/parallel_test_runner.py`'s
`_postprocess`, which does exactly this.

Usage
-----
    token="$(uv run python scripts/stamp_coverage_scope.py --record-start)"
    uv run python scripts/stamp_coverage_scope.py --expect-token "$token" [--report coverage.json]

Exit semantics
--------------
`--record-start`: 0 on success (the token is on stdout). 1 if HEAD could not be resolved -- a
real failure a caller can react to (skip the later stamp call), NOT a gate on the coverage run
itself; the marker is cleared either way.

The stamp call (no `--record-start`): always 0. Stamping is not itself a gate: a coverage-producing
recipe that failed before writing anything, or whose start conditions could not be verified, has
nothing here to report, and `find_reusable_report` already treats an unstamped report as unusable
-- silently doing nothing here degrades to "run the tests", never to a false reuse.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess  # nosec B404 - fixed `git` argv, no shell, no caller-supplied strings
import sys


DEFAULT_REPORT = "coverage.json"
DEFAULT_START_MARKER = ".coverage-scope-start.json"


def _head() -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    result = subprocess.run(  # noqa: S603  # nosec B603 - resolved executable, literal subcommand
        [git, "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _dirty() -> bool:
    """True unless the working tree is provably clean. Unresolvable git -> treated as dirty:

    a run whose cleanliness cannot be established gets the same "do not stamp" treatment as one
    known to be dirty, never the more permissive "assume clean".
    """
    git = shutil.which("git")
    if git is None:
        return True
    result = subprocess.run(  # noqa: S603  # nosec B603 - resolved executable, literal subcommand
        [git, "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def record_start(marker_path: Path) -> int:
    """Clear any existing marker FIRST -- unconditionally, before anything can fail -- so a
    failed or skipped record-start never leaves a stale marker (from an earlier, possibly
    unrelated or aborted run) for a later stamp call to misread as describing THIS run.
    """
    marker_path.unlink(missing_ok=True)
    head = _head()
    if not head:
        print("⚠️  could not resolve HEAD; not recording a start marker.", file=sys.stderr)  # noqa: T201
        return 1
    token = secrets.token_hex(16)
    marker_path.write_text(json.dumps({"head": head, "dirty": _dirty(), "token": token}), encoding="utf-8")
    print(token)  # noqa: T201 -- the ONLY stdout output; callers capture it via $(...)
    return 0


def _load_and_consume_start(marker_path: Path) -> dict[str, object] | None:
    """Read the start marker and delete it -- one-shot, so a stale marker from an aborted or
    unrelated run can never be picked up by a later, unrelated stamp attempt.
    """
    if not marker_path.is_file():
        return None
    try:
        start = json.loads(marker_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        start = None
    finally:
        marker_path.unlink(missing_ok=True)
    return start if isinstance(start, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stamp a full-suite coverage.json with the tree it was measured against.")
    parser.add_argument("--report", default=DEFAULT_REPORT, help="coverage json report to stamp")
    parser.add_argument(
        "--start-marker",
        default=DEFAULT_START_MARKER,
        help="where --record-start writes, and the stamp step reads and consumes, the pre-run tree state",
    )
    parser.add_argument(
        "--record-start",
        action="store_true",
        help="record HEAD + working-tree cleanliness NOW, before the coverage-producing step runs, and print a token identifying this run to stdout",
    )
    parser.add_argument(
        "--expect-token",
        help="the token THIS run's OWN --record-start printed; the stamp refuses unless the marker's token matches, so a concurrent run overwriting the marker is detected rather than trusted",
    )
    args = parser.parse_args(argv)

    marker_path = Path(args.start_marker)
    if args.record_start:
        return record_start(marker_path)

    path = Path(args.report)
    if not path.is_file():
        print(f"⚠️  {path} not found; nothing to stamp.", file=sys.stderr)  # noqa: T201
        _load_and_consume_start(marker_path)  # still consume a marker so it never leaks forward
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"⚠️  {path} is not valid JSON ({exc}); nothing to stamp.", file=sys.stderr)  # noqa: T201
        _load_and_consume_start(marker_path)
        return 0
    if not isinstance(data, dict) or "num_branches" not in (data.get("totals") or {}):
        print(f"⚠️  {path} carries no branch data; nothing to stamp.", file=sys.stderr)  # noqa: T201
        _load_and_consume_start(marker_path)
        return 0

    start = _load_and_consume_start(marker_path)
    if start is None or "head" not in start or "token" not in start:
        print(f"⚠️  no start marker at {marker_path} (call --record-start before the coverage-producing step ran); nothing to stamp.", file=sys.stderr)  # noqa: T201
        return 0
    if start.get("token") != args.expect_token:
        print(f"⚠️  {marker_path} does not match --expect-token -- a concurrent run may have overwritten it; nothing to stamp.", file=sys.stderr)  # noqa: T201
        return 0
    if start.get("dirty"):
        print("⚠️  the working tree was already dirty when this run started; a dirty measurement has no exact tree to stamp it with.", file=sys.stderr)  # noqa: T201
        return 0

    head = _head()
    if not head:
        print("⚠️  could not resolve HEAD; nothing to stamp.", file=sys.stderr)  # noqa: T201
        return 0
    if start["head"] != head:
        print(f"⚠️  HEAD moved during the run ({start['head']} -> {head}); nothing to stamp.", file=sys.stderr)  # noqa: T201
        return 0

    data["_phaze_scope"] = {"kind": "full", "head": head}
    path.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    print(f"✅ Stamped {path} for HEAD {head}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
