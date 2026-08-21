"""Per-bead branch-coverage gate: a bead may not LOWER branch coverage on a file it touched.

phaze-bk9el.21, operator decision 2026-08-21. Question as put: "Branch coverage is off, and runs
4-8 points under line coverage on the refactor targets. What should this epic do about it?" Answer
as given (selected option label, verbatim): "Enable it, gate the refactor targets only
(Recommended)". Durable record: bead phaze-bk9el.21.

WHY A PER-BEAD GATE RATHER THAN A REPO-WIDE ONE. Branch coverage sits below the line figure on
most files in this repo, so a repo-wide branch floor would fail on day one and the backfill would
dwarf the epic it was meant to protect. The repo-wide floors therefore stay on LINES (95% total,
90% per module -- see scripts/coverage_floor.py), and branches are gated only where a change can
actually regress them: the files the bead in front of you edited.

That narrow scope is not a compromise, it is the point. Decomposing a long function, flattening a
nest, or splitting a file are all operations where EVERY LINE STILL EXECUTES and only the branch
combinations change; line coverage is structurally unable to see the regression, and a repo-wide
average is far too coarse to. Measured on this repo (phaze-bk9el.21, partial run over each file's
primary exercisers): job_runner.py 97.12% lines / 89.13% branches, services/analysis.py 97.91% /
93.97%, services/video_audio.py 94.62% / 87.50%. All three pass the 95%/90% line gates while
sitting below them on branches.

THE RULE (criterion 5): raising branch coverage on a touched file is welcome, holding it steady is
acceptable, LOWERING it fails. A file the bead did not touch is out of scope for that bead's check.

Usage
-----
    just branch-check                     # after a coverage run, against the recorded baseline
    just branch-check --base-ref main     # choose the ref the touched-file set is diffed against
    just branch-check --write-baseline    # record the current numbers as the baseline (phaze-bk9el.1)
    just branch-check --allow-missing-baseline   # phaze-bk9el.1 ONLY -- see "ON THE MISSING BASELINE"

or directly::

    uv run python scripts/branch_coverage_check.py [--coverage coverage.json]
                                                   [--baseline .coverage-baseline.json]
                                                   [--base-ref REF] [--file PATH ...]
                                                   [--write-baseline]

Inputs
------
``coverage.json``
    A ``coverage json`` report produced with branch coverage on. ``just test-cov`` and
    ``just coverage-combine`` both write one; ``branch = true`` in pyproject means any coverage
    run in this repo carries branch data.
``.coverage-baseline.json``
    The committed baseline (phaze-bk9el.1). ``{"files": {path: {"percent_branches_covered": float,
    "num_branches": int}}}`` plus provenance. A file ABSENT from the baseline is treated as new and
    cannot regress.

Exit semantics (FAIL CLOSED, without exception)
----------------------------------------------
0   -- no touched file lowered its branch coverage; or nothing tracked was touched; or
       ``--allow-missing-baseline`` was passed and no baseline exists.
1   -- at least one touched file's branch coverage is below its baseline.
2   -- the check could not perform a comparison: coverage.json missing/unparseable, carrying no
       branch data at all (which would otherwise read as "0 regressions" for the wrong reason), or
       NO BASELINE recorded and no ``--allow-missing-baseline``.

ON THE MISSING BASELINE, because the obvious design is wrong. There is a real circularity here:
the baseline is produced by ``phaze-bk9el.1``, which is itself a bead that has to pass this check,
so a missing baseline cannot simply be fatal for everyone. The tempting fix -- exit 0 with a
warning banner -- is the one this epic specifically cannot afford. ``exit 0`` is what ``just check``
and CI consume; a banner is for a human reading a transcript, and nobody reads banners in a
20-minute log. "Emit a success signal that measured nothing" is the exact defect class this
molecule exists to close: ``phaze-jnj90`` was a gate that produced no coverage and ``phaze-nqawu``
was a submit that ran no tests, and both looked green.

It is also not only ``.1`` at risk. Twelve wave-2 beads run this check, and from inside the tool a
baseline that "legitimately does not exist yet" is indistinguishable from one that should be there
and is missing for a mundane reason -- a bad fetch, a branch that does not carry the file yet, a
worktree cut before ``.1`` landed. So the default is fatal, and the exemption is an explicit flag
that appears at the call site where a reviewer can see it. Only ``phaze-bk9el.1`` passes it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess  # nosec B404 - fixed `git` argv, no shell, no caller-supplied strings
import sys


DEFAULT_COVERAGE = "coverage.json"
DEFAULT_BASELINE = ".coverage-baseline.json"
# Only source files can regress a bead's branch coverage; tests are excluded from the coverage
# report anyway (`omit = ["tests/*"]`), so a touched test file simply never appears here.
TRACKED_PREFIX = "src/phaze/"


def _git(*args: str) -> str:
    """Run ``git`` and return stdout, or "" if the command failed.

    Failure is a normal outcome here, not an error: ``--base-ref`` can name a ref this worktree
    has never fetched, and a shallow bead worktree may have no merge-base with it. Falling back
    to "no files from that diff" degrades the check to the other two diffs rather than killing
    the gate over a ref name.
    """
    git = shutil.which("git")
    if git is None:
        return ""
    result = subprocess.run(  # noqa: S603  # nosec B603 - resolved executable, literal subcommands
        [git, *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def touched_files(base_ref: str) -> list[str]:
    """Return the tracked source files this working tree changes relative to ``base_ref``.

    Three diffs are unioned deliberately. A bead is checked mid-flight as often as at submit time,
    so committed work (``base_ref...HEAD``), staged work and unstaged work must all count -- a
    check that saw only committed changes would report "nothing touched" for the developer who has
    not committed yet, which is precisely when the answer is most useful.
    """
    paths: set[str] = set()
    merge_base = _git("merge-base", base_ref, "HEAD").strip()
    if merge_base:
        paths.update(_git("diff", "--name-only", merge_base, "HEAD").split())
    paths.update(_git("diff", "--name-only", "HEAD").split())
    paths.update(_git("diff", "--name-only", "--cached").split())
    return sorted(p for p in paths if p.startswith(TRACKED_PREFIX) and p.endswith(".py"))


def branch_percent(summary: dict[str, float]) -> float | None:
    """Branch coverage for one coverage.json ``summary``; ``None`` when the file has no branches.

    A file with zero branches (straight-line module, constants, protocol definitions) is not
    100% covered and is not 0% covered -- it is not measurable, and reporting either number would
    make it look like it had moved when a refactor adds its first branch.
    """
    if not summary.get("num_branches"):
        return None
    if "percent_branches_covered" in summary:
        return float(summary["percent_branches_covered"])
    return float(summary["covered_branches"]) / float(summary["num_branches"]) * 100.0


def missing_branch_lines(info: dict[str, object]) -> list[int]:
    """The source lines whose branches are not fully taken -- criterion 6.

    coverage.json records each uncovered arc as ``[source_line, destination]``; a developer can act
    on the source line, so that is what is reported, deduplicated and ordered. A percentage alone
    tells you that you regressed and nothing about where.
    """
    arcs = info.get("missing_branches")
    if not isinstance(arcs, list):
        return []
    lines: set[int] = set()
    for arc in arcs:
        if isinstance(arc, list) and arc:
            lines.add(int(arc[0]))
    return sorted(lines)


def load_coverage(path: Path) -> dict[str, object]:
    if not path.exists():
        print(f"❌ {path} not found. Run a coverage-producing suite first (`just test-cov`).")  # noqa: T201
        raise SystemExit(2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"❌ {path} is not valid JSON ({exc}).")  # noqa: T201
        raise SystemExit(2) from exc
    if not isinstance(data, dict) or "files" not in data:
        print(f"❌ {path} has no 'files' section — not a coverage json report.")  # noqa: T201
        raise SystemExit(2)
    # FAIL CLOSED on a branch-less report. Without this, a run that forgot branch coverage reports
    # "no regressions" for every file, which is the same green as a genuinely clean bead.
    totals = data.get("totals") or {}
    if "num_branches" not in totals:
        print(f"❌ {path} carries no branch data — was it produced with `branch = true` / --cov-branch?")  # noqa: T201
        raise SystemExit(2)
    return data


def _summary_of(info: dict[str, object]) -> dict[str, float]:
    """One file entry's ``summary`` block. A report whose entry has none is treated as unmeasurable."""
    summary = info.get("summary")
    return summary if isinstance(summary, dict) else {}


def _files_of(data: dict[str, object]) -> dict[str, dict[str, object]]:
    """The report's ``files`` mapping, narrowed. ``load_coverage`` has already refused a report without one."""
    files = data["files"]
    if not isinstance(files, dict):
        print("❌ coverage.json's 'files' section is not an object — not a coverage json report.")  # noqa: T201
        raise SystemExit(2)
    return files


def write_baseline(data: dict[str, object], baseline_path: Path, base_ref: str) -> int:
    files = _files_of(data)
    payload = {
        "_comment": "Branch-coverage baseline (phaze-bk9el.21 criterion 5). Regenerate with `just branch-check --write-baseline` after a FULL-suite coverage run; a partial run records partial numbers and would fail every later bead for the wrong reason.",
        "commit": _git("rev-parse", "HEAD").strip(),
        "base_ref": base_ref,
        "files": {
            path: {
                "percent_branches_covered": branch_percent(_summary_of(info)),
                "num_branches": _summary_of(info).get("num_branches", 0),
            }
            for path, info in sorted(files.items())
            if branch_percent(_summary_of(info)) is not None
        },
    }
    baseline_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"✅ Wrote branch-coverage baseline for {len(payload['files'])} files to {baseline_path}")  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-bead branch-coverage regression check.")
    parser.add_argument("--coverage", default=DEFAULT_COVERAGE, help="coverage json report to read")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE, help="recorded baseline to compare against")
    parser.add_argument("--base-ref", default="main", help="ref the touched-file set is diffed against")
    parser.add_argument("--file", action="append", default=[], help="check this file instead of the git-derived set (repeatable)")
    parser.add_argument("--write-baseline", action="store_true", help="record the current report as the baseline")
    parser.add_argument(
        "--allow-missing-baseline",
        action="store_true",
        help="report current figures and exit 0 when no baseline exists (phaze-bk9el.1 only -- see the module docstring)",
    )
    args = parser.parse_args(argv)

    data = load_coverage(Path(args.coverage))
    if args.write_baseline:
        return write_baseline(data, Path(args.baseline), args.base_ref)

    files = _files_of(data)
    checked = args.file or touched_files(args.base_ref)
    if not checked:
        print(f"✅ No tracked {TRACKED_PREFIX}*.py files changed against '{args.base_ref}' — nothing to check.")  # noqa: T201
        return 0

    baseline_path = Path(args.baseline)
    baseline: dict[str, dict[str, float]] = {}
    have_baseline = baseline_path.exists()
    if have_baseline:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")).get("files", {})
    elif args.allow_missing_baseline:
        print(f"⚠️  NOT A COMPARISON: no baseline at {baseline_path} (recorded by phaze-bk9el.1).")  # noqa: T201
        print("   The figures below are this run's branch coverage, with nothing to compare them to.")  # noqa: T201
        print("   Exiting 0 only because --allow-missing-baseline was passed.")  # noqa: T201
    else:
        # FAIL CLOSED. If the check could not perform a comparison, it did not pass.
        print(f"❌ No branch-coverage baseline at {baseline_path} — cannot tell whether this bead lowered anything.")  # noqa: T201
        print("   The baseline is recorded by phaze-bk9el.1 (`just branch-check --write-baseline`).")  # noqa: T201
        print("   If this bead is phaze-bk9el.1 itself, pass --allow-missing-baseline; nothing else should.")  # noqa: T201
        raise SystemExit(2)

    print(f"Branch coverage for the {len(checked)} tracked file(s) this bead touched (base-ref '{args.base_ref}'):")  # noqa: T201
    regressions: list[str] = []
    for path in checked:
        info = files.get(path)
        if info is None:
            print(f"   ―― {path}: not present in the coverage report (deleted, renamed, or never imported)")  # noqa: T201
            continue
        current = branch_percent(_summary_of(info))
        if current is None:
            print(f"   ―― {path}: no branches to measure")  # noqa: T201
            continue
        before = baseline.get(path, {}).get("percent_branches_covered") if have_baseline else None
        uncovered = missing_branch_lines(info)
        detail = f"uncovered branch lines: {', '.join(str(n) for n in uncovered)}" if uncovered else "all branches covered"
        if before is None:
            marker = "✅" if not have_baseline else "🆕"
            print(f"   {marker} {path}: {current:6.2f}%  ({detail})")  # noqa: T201
            continue
        delta = current - float(before)
        # Float equality is the wrong test on two percentages computed from different runs; a
        # hundredth of a point is below the reporting precision and is not a regression.
        if delta < -0.005:
            regressions.append(path)
            print(f"   ❌ {path}: {current:6.2f}% vs baseline {float(before):6.2f}%  ({delta:+.2f}) — {detail}")  # noqa: T201
        else:
            print(f"   ✅ {path}: {current:6.2f}% vs baseline {float(before):6.2f}%  ({delta:+.2f}) — {detail}")  # noqa: T201

    if regressions:
        print(f"\n❌ {len(regressions)} file(s) lowered branch coverage against the baseline: {', '.join(regressions)}")  # noqa: T201
        print("   Cover the branch lines listed above, or say in the bead why the branch is unreachable.")  # noqa: T201
        return 1
    if have_baseline:
        print("\n✅ No touched file lowered its branch coverage against the baseline.")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
