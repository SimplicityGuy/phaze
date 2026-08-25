"""Select the tests a bead's change actually exercises, or refuse to guess (phaze-pv3kk).

``just test-fast`` runs this to decide what ``bh work check`` / ``bh work submit`` /
``bh work merge`` execute. It wraps ``repowise impacted-tests``, whose per-test coverage map is the
selector the operator chose on 2026-08-25: *"use repowise tests_to_run for phaze-pv3kk. While this
may produce longer than 5 minutes, it is selecting the right set of tests for the change."*

**Everything hard about this is the refusing, not the selecting.** ``repowise impacted-tests``
answers in three tiers and is scrupulous about labelling which one answered -- but it reports every
degraded state as a *warning on stderr and exit 0*. Piped naively into pytest, each of the states
below runs zero tests and reports green, which is the ``phaze-jnj90`` defect exactly: a gate that
exits 0 having measured nothing. So this script's job is to turn each of them into a verdict.

THREE VERDICTS, on stdout, with the exit code carrying the same thing so a caller can branch on
either:

    run <n>           exit 0   -- a measured selection of n node ids was written to --out
    escalate <why>    exit 3   -- the selector cannot speak for this change; run the FULL suite
    fail <why>        exit 1   -- something is wrong that neither running nor escalating fixes

ESCALATE IS NOT A FAILURE AND IT IS NOT "RUNNING THE GUESS". The guesses are discarded; the full
suite replaces them. It is what repowise's own docstring prescribes for its lower tiers ("Nothing
said anything. Run the full suite."), and it is the only response that is never weaker than the gate
it replaces. A hard failure would leave the seat with no path except running the full suite by
hand -- the same runtime, one manual step, and an invitation to skip it.

=== THE SIX WAYS THIS GOES WRONG, ALL MEASURED 2026-08-25 AGAINST repowise 0.45.0 ===

**A. THE COVERAGE MAP GOES STALE, AND `repowise update` DOES NOT FIX IT.** This is the one that
looks handled and is not. The map is built by ``repowise coverage add`` from a
``pytest --cov --cov-context=test`` run -- ``just repowise-coverage``, about 21 minutes -- and is a
DIFFERENT artifact from the wiki/health index that ``repowise update`` refreshes. Running
``repowise update`` in this path would produce a freshly-updated index over an untouched map, which
reads as fixed and is not.

Measured: the map's own ``test_map.ingested_commit_sha`` was ``a3fd169a`` (2026-08-22 08:21) while
``origin/main`` was ``190a9e30`` (2026-08-24 20:52) -- **103 commits**, with **40 of 272
``src/phaze`` files** and 46 test files changed in between.

The check below is TARGET-AWARE, not a commit-equality test, because ``ingested_commit_sha != HEAD``
is the trap CLAUDE.md names: it is true for every bead by construction (the bead just edited the
file) and so means nothing. What matters is whether *someone else's* work moved a file after the map
measured it -- ``git log <map_sha>..<base> -- <file>`` -- because then the recorded line numbers no
longer point at the same code and the line intersection is arithmetic on stale coordinates.

*Consequence worth stating out loud:* this gate's usefulness is a direct function of how recently
``just repowise-coverage`` ran. At 103 commits of drift, a lot of changes escalate. That makes the
21-minute refresh a periodic maintenance obligation rather than an optional nicety, and it is the
honest price of change-driven selection.

**B. THE TIERS ARE NOT INTERCHANGEABLE.** ``impacted_tests`` carries ``via: "coverage"`` -- the only
tier that knows LINES and proves execution. ``inferred_tests`` carries four different ``via`` values
and repowise's own docstring says of them: *"All are file-level and all over-claim; none may be read
as coverage."* Exactly one of the four is sound here:

* ``changed-test``   -- the changed file IS a test file. Not a guess about coverage at all; you
                        edited that test, so it runs. ACCEPTED.
* ``call-graph``     -- a test whose calls reach the file.        } candidates, not measurements.
* ``import-graph``   -- a test that only imports it.              } ESCALATE.
* ``filename-pattern``-- a name-shaped guess.                     }

**C. AN EMPTY SELECTION IS NOT A PASS.** A file with no coverage rows selects nothing. Guarded twice
over, because the two cases are different: the always-run floor (``--floor``) means the gate can
never execute zero tests, and an empty *selection* against a non-empty diff with nothing escalated is
a hard failure rather than a green run.

**D. `unknown_files` IS THE NORMAL CASE HERE, NOT THE EDGE CASE.** phaze renders its entire UI from
Jinja templates, and the map has no rows for ``.html``, ``pyproject.toml``, the justfile or any YAML.
Measured on a sampled range: 6 unknown files, two of them templates. A change to
``_diff_row.html`` selects ZERO tests. Escalation is therefore common, and that is correct behaviour
rather than a defect to tune away.

**E. `test_id` IS NOT A PYTEST NODE ID.** The map stores coverage CONTEXTS, so ids arrive suffixed:
``tests/review/routers/test_proposals.py::test_approve_proposal|run``. Measured: pytest exits **4**
(usage error) on that form, and 4 again when it is mixed with valid paths -- loud rather than silent,
which is the good direction, but it has to be stripped. Note this also means repowise's own
documented pipeline, ``--format list | xargs pytest``, does not work for the covered tier;
``--format list`` emits precisely these suffixed ids mixed with bare file paths. Hence ``--format
json`` here.

**F. THERE IS NO INDEX IN A bh WORKTREE, AND THAT FAILS SILENTLY.** The map is keyed by repo PATH and
lives in the main clone. Run from ``wt/bead/issue/<id>``, ``impacted-tests`` returns
``no_index: true``, an empty selection and exit 0 -- in EVERY bead worktree, i.e. the default state
for every bead. The fix is available only because bh worktrees are ``git worktree``s of the main
clone and share its object store, so the clone can resolve a bead's own shas: this script derives it
from ``git rev-parse --git-common-dir`` and asks repowise about the range there.

That in turn means only COMMITTED work is visible, so a dirty tree is refused rather than silently
under-selected. "Commit before check" is already the dispatch protocol.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess  # nosec B404 - resolved `git` / `repowise` argv, no shell, no caller-supplied strings
import sys
from typing import Any


# repowise's inferred tier, split by whether the label is a claim about coverage or a fact about the
# diff. Only `changed-test` is a fact: it says "you edited this test file", which needs no map.
SOUND_INFERENCE = frozenset({"changed-test"})
GUESS_INFERENCE = frozenset({"call-graph", "import-graph", "filename-pattern"})

# The coverage-context suffix on every mapped test id (see failure mode E).
CONTEXT_SUFFIXES = ("|run", "|setup", "|teardown")


class Refuse(Exception):
    """Raised with a verdict this script must report instead of a selection."""

    def __init__(self, verdict: str, reason: str) -> None:
        super().__init__(reason)
        self.verdict = verdict
        self.reason = reason


def _resolve(tool: str) -> str:
    """Absolute path to ``tool``, or a refusal — a partial path is both a lint finding and a hazard."""
    found = shutil.which(tool)
    if found is None:
        raise Refuse("fail", f"{tool} is not on PATH")
    return found


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stdout, raising ``Refuse`` on a nonzero exit."""
    result = subprocess.run(  # noqa: S603  # nosec B603 - resolved executable, literal subcommands
        [_resolve("git"), "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise Refuse("fail", f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _repowise_json(main_clone: Path, *args: str) -> dict[str, Any]:
    """Run a repowise subcommand with ``--format json`` and parse stdout.

    repowise writes diagnostics to stderr and keeps stdout clean for machine formats, so stdout is
    parsed alone. A nonzero exit OR unparseable stdout is a refusal rather than an empty selection:
    "could not ask" must never render as "nothing to run" (failure mode C).
    """
    result = subprocess.run(  # noqa: S603  # nosec B603 - resolved executable, literal subcommands
        [_resolve("repowise"), *args, "--path", str(main_clone), "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise Refuse("escalate", f"`repowise {args[0]}` exited {result.returncode}: {result.stderr.strip()[:200]}")
    try:
        parsed: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise Refuse("escalate", f"`repowise {args[0]}` emitted unparseable JSON: {exc}") from exc
    return parsed


def strip_context(test_id: str) -> str:
    """Turn a coverage-context id into a pytest node id (failure mode E).

    ``tests/x.py::test_y|run`` -> ``tests/x.py::test_y``. Only the known context phases are stripped;
    an unrecognised suffix is left alone so it reaches pytest and fails loudly as a usage error
    rather than being silently mangled into a node id that collects something else.
    """
    for suffix in CONTEXT_SUFFIXES:
        if test_id.endswith(suffix):
            return test_id[: -len(suffix)]
    return test_id


def assert_clean_worktree(repo: Path) -> None:
    """Refuse on uncommitted changes -- they are invisible to the main clone's diff (failure mode F)."""
    if _git(repo, "status", "--porcelain"):
        raise Refuse(
            "fail",
            "the worktree has uncommitted changes, which the main clone's index cannot see. "
            "Commit first (that is already the dispatch protocol), then re-run.",
        )


def stale_sources(repo: Path, map_sha: str, base: str, sources: set[str]) -> list[str]:
    """Return the changed files another commit moved after the map measured them (failure mode A).

    ``base`` is this bead's merge-base, so the range ``map_sha..base`` deliberately EXCLUDES the
    bead's own commits: the question is never "did this file change" -- the bead changed it, that is
    why it is here -- but "did the coverage rows stop describing this file because of somebody
    else's work". An empty result means the map's line numbers still point at the same code.
    """
    if _git(repo, "cat-file", "-t", map_sha) != "commit":
        raise Refuse("escalate", f"the coverage map names commit {map_sha[:8]}, which this repo cannot resolve")
    moved = []
    for source in sorted(sources):
        if _git(repo, "log", "--oneline", f"{map_sha}..{base}", "--", source):
            moved.append(source)
    return moved


def classify(report: dict[str, Any]) -> tuple[list[str], set[str]]:
    """Turn one ``repowise impacted-tests --format json`` report into node ids, or raise ``Refuse``.

    PURE: no git, no subprocess, no clock. Every judgement about what may be RUN versus what must
    ESCALATE lives here and nowhere else, so ``tests/shared/test_fast_gate.py`` can exercise the
    shipped logic over synthetic reports rather than a guard re-implementing it in parallel. Returns
    the node ids to run plus the source files the coverage tier spoke for -- the caller needs the
    latter for the staleness check, which is the one judgement that does need git.
    """
    if report.get("no_index"):
        raise Refuse("escalate", "repowise reports no index for this repo")
    if report.get("map_empty"):
        raise Refuse("escalate", "repowise reports an empty test-to-code map")

    unknown = [str(u) for u in (report.get("unknown_files") or [])]
    if unknown:
        raise Refuse(
            "escalate",
            f"{len(unknown)} changed file(s) have no coverage, no test reaching them and no paired test: "
            f"{', '.join(unknown[:5])}" + (" \u2026" if len(unknown) > 5 else ""),
        )

    inferred = list(report.get("inferred_tests") or [])
    guesses = [g for g in inferred if g.get("via") in GUESS_INFERENCE]
    if guesses:
        vias = sorted({str(g["via"]) for g in guesses})
        files = sorted({str(g["source_file"]) for g in guesses})
        raise Refuse(
            "escalate",
            f"{len(guesses)} candidate test(s) for {len(files)} file(s) are inference, not coverage ({', '.join(vias)})",
        )

    unlabelled = [g for g in inferred if g.get("via") not in GUESS_INFERENCE | SOUND_INFERENCE]
    if unlabelled:
        # A `via` this script has never seen is a repowise upgrade, not a tier to assume is safe.
        raise Refuse("escalate", f"unrecognised inference label(s) {sorted({str(g.get('via')) for g in unlabelled})} — re-read the tiers")

    impacted = list(report.get("impacted_tests") or [])
    covered_sources: set[str] = set()
    for entry in impacted:
        covered_sources.update(str(s) for s in entry.get("source_files") or [])

    node_ids = [strip_context(str(entry["test_id"])) for entry in impacted]
    node_ids += [str(g["test_file"]) for g in inferred if g.get("via") in SOUND_INFERENCE]

    # Dedup, order-stable, so the selection is reproducible run to run. Written as a loop rather
    # than the `not (n in seen or seen.add(n))` one-liner, which mypy rejects here (`add` returns
    # None) — the repo's strict config is right and the idiom is not worth a type: ignore.
    seen: set[str] = set()
    deduped: list[str] = []
    for node_id in node_ids:
        if node_id not in seen:
            seen.add(node_id)
            deduped.append(node_id)

    changed_files = int(report.get("changed_files", 0))
    if changed_files and not deduped:
        raise Refuse(
            "fail",
            f"{changed_files} file(s) changed and the map named no test for any of them, yet nothing escalated. "
            "That is 'exit 0 having measured nothing' — refusing to report a verdict.",
        )
    return deduped, covered_sources


def select(repo: Path, base_ref: str) -> tuple[list[str], list[str]]:
    """Return ``(node_ids, notes)`` for the change, or raise ``Refuse``."""
    notes: list[str] = []
    assert_clean_worktree(repo)

    main_clone = Path(_git(repo, "rev-parse", "--git-common-dir")).resolve().parent
    if not (main_clone / ".repowise").is_dir():
        raise Refuse("escalate", f"no repowise index at {main_clone} — run `repowise init` there")
    notes.append(f"index: {main_clone}")

    head = _git(repo, "rev-parse", "HEAD")
    base = _git(repo, "merge-base", base_ref, "HEAD")
    notes.append(f"range: {base[:8]}..{head[:8]} (vs {base_ref})")

    status = _repowise_json(main_clone, "coverage", "status")
    test_map = status.get("test_map") or {}
    map_sha = str(test_map.get("ingested_commit_sha") or "")
    if not test_map.get("pair_count"):
        raise Refuse("escalate", "the per-test coverage map is empty — run `just repowise-coverage`")
    if not map_sha:
        raise Refuse("escalate", "the coverage map records no commit, so its freshness cannot be established")
    notes.append(f"map: {test_map['pair_count']} pairs at {map_sha[:8]} ({test_map.get('ingested_at', 'unknown')})")

    report = _repowise_json(main_clone, "impacted-tests", f"{base}..{head}")
    node_ids, covered_sources = classify(report)
    notes.append(f"changed files: {report.get('changed_files', 0)}")

    moved = stale_sources(repo, map_sha, base, covered_sources)
    if moved:
        raise Refuse(
            "escalate",
            f"the coverage map predates other commits that moved {len(moved)} of the changed file(s), so its line "
            f"numbers no longer describe them: {', '.join(moved[:5])}" + (" \u2026" if len(moved) > 5 else ""),
        )

    n_covered = len(report.get("impacted_tests") or [])
    notes.append(f"selected: {n_covered} coverage-backed + {len(node_ids) - n_covered} changed-test")
    return node_ids, notes


def read_floor(path: Path) -> list[str]:
    """Read the always-run floor, ignoring comments and blanks (failure mode C)."""
    if not path.is_file():
        raise Refuse("fail", f"the always-run floor is missing: {path}")
    entries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    floor = [e for e in entries if e and not e.startswith("#")]
    if not floor:
        raise Refuse("fail", f"the always-run floor {path} names nothing — the gate could run zero tests")
    return floor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main", help="integration ref to select against (default: origin/main)")
    parser.add_argument("--repo", default=".", help="worktree root (default: cwd)")
    parser.add_argument("--floor", default="tests/fast_floor.txt", help="always-run floor manifest")
    parser.add_argument("--out", required=True, help="file to write the pytest arguments to, one per line")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        floor = read_floor(repo / args.floor)
        node_ids, notes = select(repo, args.base)
    except Refuse as refusal:
        print(f"{refusal.verdict} {refusal.reason}")  # noqa: T201
        return {"escalate": 3, "fail": 1}[refusal.verdict]

    for note in notes:
        print(f"  {note}", file=sys.stderr)  # noqa: T201

    # The floor goes FIRST so that a truncated transcript still shows the gate ran something real.
    selection = floor + [n for n in node_ids if n not in set(floor)]
    Path(args.out).write_text("\n".join(selection) + "\n", encoding="utf-8")
    print(f"run {len(selection)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
