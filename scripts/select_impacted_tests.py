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

FOUR VERDICTS, on stdout, with the exit code carrying the same thing so a caller can branch on
either:

    run <n>           exit 0   -- a measured selection of n node ids was written to --out
    escalate <why>    exit 3   -- the selector cannot speak for this change; run the FULL suite
    fail <why>        exit 1   -- something is wrong that neither running nor escalating fixes
    docs <n>          exit 4   -- every changed path is prose; run the PROSE GUARDS, not the suite

Exit 4 rather than 2 is deliberate: ``argparse`` exits **2** on a usage error, so a mistyped flag
would print a usage message and hand the caller the code that means "do not run the suite". A
weakened gate must never be reachable by accident -- that is the ``phaze-jnj90`` defect with the
argument parser holding the knife.

ESCALATE IS NOT A FAILURE AND IT IS NOT "RUNNING THE GUESS". The guesses are discarded; the full
suite replaces them. It is what repowise's own docstring prescribes for its lower tiers ("Nothing
said anything. Run the full suite."), and it is the only response that is never weaker than the gate
it replaces. A hard failure would leave the seat with no path except running the full suite by
hand -- the same runtime, one manual step, and an invitation to skip it.

=== THE DOCS-ONLY PATH (phaze-fqfds) ===

A Markdown file has no coverage rows, so before this it demoted to ``unknown_files`` and the whole
diff ESCALATED -- correct for an unmeasurable *code* file, exactly wrong for a file no unit test
executes. Measured 2026-08-25: six prose-only beads paid a ~21-minute full suite each, about two
hours, plus a re-run per rebase (a tree change invalidates the verdict ledger's key).

OPERATOR DECISION, 2026-08-25. Question as put: whether a docs-only change should run no tests at
all, or no tests EXCEPT the docs-reading guards. Answer as given, verbatim: *"for docs only
changes, no tests are needed!"*, clarified when it was read back as a prohibition: *"i answered
phaze-fqfds by stating if tests run, ok; if they don't also fine."* Durable record: bead
``phaze-fqfds``'s comments.

**That answer is PERMISSION, not a mandate, and the distinction is the whole reason this file says
so.** "No tests are needed" lifts the requirement to run the suite; it does not forbid running
anything. An earlier draft of this docstring recorded it as a prohibition -- accurate quotation,
right date, durable record present, and still a misstatement of what was decided. It was caught
only because the operator read it back. That is ADR-0012 rule 2's own failure mode occurring inside
the record that rule governs, and the lesson generalises past this bead: **a correctly formatted
citation can still misstate the decision, and the only check is the person who decided reading it
back.** So what runs here is an ENGINEERING choice made under that permission, and it can be
revisited on engineering grounds without going back to the operator.

**THE CHOICE MADE, AND WHY IT IS NOT THE OBVIOUS ONE.** A docs-only diff runs the PROSE FLOOR --
``tests/docs_floor.txt``, every test module that reads tracked prose -- and nothing else. Measured
2026-08-25 on seat ``docsgate``: the floor is **193 tests in 9.4-9.9 s** against the full suite's
~21 minutes, and against **24 tests in 5.12 s** for the three guards this was first scoped to.
Startup dominates at that size, so the COMPLETE set costs about 4.5 s more than a curated three and
under 1% of what escalating costs.

That measurement is what rules out the middle option. Curating three modules --
``test_adr_citation_resolution.py``, ``test_operator_attribution_citations.py``,
``test_adr_numbering.py`` -- would have been the worst of the three choices, not a compromise:
``docs/runbook.md``, ``docs/configuration.md`` and ``docs/k8s-burst.md`` are ALSO guarded, by
``test_docs_beui03.py``, ``test_docs_ia_current.py`` and ``test_k8s_runbook.py`` (which parses the
runbook's YAML fences), and a curated three would sail past a break in any of them while the
transcript announced that "the prose guards ran". A gate whose output overstates its own coverage
is the defect this repo has paid for twice.

Running NOTHING was the other real option and is defensible -- it is simpler, and it has no
manifest to drift. It was rejected because 9.6 s is not a saving worth having, and because the
gap it opens is not hypothetical: ``test_adr_citation_resolution.py`` exists because of
``phaze-f70y9``, where eight bare "ADR-0014" citations came to resolve to the wrong document after
a renumber and were caught once, by a human reading prose. CI does not close it either --
``ci.yml``'s ``test`` job is gated on ``detect-changes.outputs.code-changed == 'true'`` and skips
on a docs-only push, leaving only pre-commit and secret scanning. Without the floor, a bad citation
lands on ``main`` and surfaces as a red gate in the NEXT bead's transcript, on prose that bead did
not write.

**THE MANIFEST IS DERIVED, NOT CURATED, WHICH IS WHAT ANSWERS THE DRIFT OBJECTION.**
``tests/shared/test_fast_gate.py`` scans every test module for a reference to a tracked prose path
and fails the build if one is missing from the floor, so a prose guard added next month joins the
floor or breaks the gate. It over-matches on purpose: a module that merely MENTIONS ``docs/`` costs
a few milliseconds in the floor, while one that reads prose and is absent from it is a silent hole.

**THE CLASSIFIER IS A STRICT ALLOW-LIST, AND THAT IS THE SUBSTANCE OF THE CHANGE.** "Not a ``.py``
file" would be catastrophic here: ``justfile``, ``pyproject.toml``, every YAML, every Jinja
template and every shell script are ALSO outside the coverage map and are all executable. Three
rules, each of which fails closed:

* **Suffix**: exactly ``.md``. Not ``.txt``, not ``LICENSE``, not "everything under ``docs/``" --
  ``docs/`` holds ``.py`` benchmarks, ``.sh`` drivers, ``.patch`` files and ``.html`` prototypes.
* **Location**: a root-level ``*.md`` (``CLAUDE.md``, ``README.md``, ``CONVENTIONS.md``) or one
  under ``docs/``, ``.planning/`` or ``design/``. ``src/``, ``tests/``, ``scripts/`` and
  ``services/`` are excluded by construction, which is the ``phaze-tlo10`` lesson the CI classifier
  learned the hard way: ``src/phaze/prompts/naming.md`` is loaded at runtime by
  ``load_prompt_template()`` and depended on for its exact placeholder lines. It is code that
  happens to end in ``.md``.
* **Mode**: every non-empty mode in the diff record must be ``100644``. That is what rejects a
  chmod to ``100755``, a symlink (``120000``), a submodule (``160000``) and a type change -- none
  of which ``--name-only`` can even show you.

And two whole-diff rules: **every** entry must qualify (one ``.py`` among twenty ``.md`` is a code
change), and an **empty** diff is not a docs change -- it falls through to the normal path, where
the always-run floor still runs.

This is deliberately NOT ``scripts/classify-changed-files.sh``, the CI skip gate. That one answers
a different question with a looser allow-list (all of ``docs/`` whatever the extension, plus
``*.txt`` and ``LICENSE``) over a caller-supplied path list that carries no modes. Sharing it would
import its looseness into the last gate before ``main`` and make widening one silently widen the
other. ``tests/shared/test_fast_gate.py`` pins this allow-list; ``tests/shared/test_change_gate.py``
pins that one.

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

# ---------------------------------------------------------------------------------------------
# The docs allow-list (phaze-fqfds). Every rule here is POSITIVE: a path is documentation only if
# it satisfies all of them. A negation ("not a .py file") would hand the skip to the justfile, to
# pyproject.toml, to every YAML and every Jinja template.
# ---------------------------------------------------------------------------------------------

DOCS_SUFFIX = ".md"

# Root-level *.md is allowed separately (CLAUDE.md, README.md, CONVENTIONS.md). These are the only
# DIRECTORIES prose lives in here. src/, tests/, scripts/ and services/ are absent on purpose:
# src/phaze/prompts/naming.md is runtime-loaded content, not documentation (phaze-tlo10).
DOCS_ROOTS: tuple[str, ...] = ("docs/", ".planning/", "design/")

# A regular, non-executable blob. Anything else in a diff record -- 100755 (chmod +x), 120000
# (symlink), 160000 (submodule) -- is not a prose edit whatever the path says.
DOCS_MODE = "100644"
EMPTY_MODE = "000000"

# Diff statuses this classifier is willing to reason about. `T` (type change) is deliberately
# absent, and so is anything unlisted: `U` (unmerged), `X` (unknown) and whatever a future git
# adds all land on "cannot classify" -> not docs -> the normal path.
CLASSIFIABLE_STATUSES = frozenset({"A", "C", "D", "M", "R"})

# `git diff --raw` emits two paths for a rename or a copy and one for everything else.
TWO_PATH_STATUSES = frozenset({"C", "R"})

# The verdict -> exit code map. `docs` is 4 and NOT 2 because argparse exits 2 on a usage error:
# a mistyped flag must not be able to mean "do not run the suite".
VERDICT_EXIT = {"escalate": 3, "fail": 1, "docs": 4}


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


def _git(repo: Path, *args: str, strip: bool = True) -> str:
    """Run git in ``repo`` and return stdout, raising ``Refuse`` on a nonzero exit.

    ``strip=False`` is for ``--raw -z``, whose records are NUL-delimited: stripping would be
    harmless today but the caller parses field-by-field and should not depend on that.
    """
    result = subprocess.run(  # noqa: S603  # nosec B603 - resolved executable, literal subcommands
        [_resolve("git"), "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise Refuse("fail", f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip() if strip else result.stdout


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


def is_docs_path(path: str) -> bool:
    """True only for a Markdown file in a directory this repo keeps prose in (phaze-fqfds).

    PURE. Three positive conditions, no negation anywhere:

    * the suffix is exactly ``.md`` -- so ``.txt``, ``LICENSE`` and every non-Markdown file under
      ``docs/`` (which holds ``.py`` benchmarks, ``.sh`` drivers, ``.patch`` files and ``.html``
      prototypes) all fail;
    * it is a root-level file, or lives under one of :data:`DOCS_ROOTS`;
    * it contains no ``..`` component -- git does not emit one, and a classifier that would accept
      ``docs/../src/phaze/prompts/naming.md`` is one rewrite of its caller away from doing so.

    ``src/``, ``tests/``, ``scripts/`` and ``services/`` are not in :data:`DOCS_ROOTS`, which is the
    phaze-tlo10 lesson stated positively: a doc-extension file inside a shipped tree is code.
    """
    if not path.endswith(DOCS_SUFFIX) or path == DOCS_SUFFIX:
        return False
    if ".." in path.split("/"):
        return False
    if "/" not in path:
        return True
    return path.startswith(DOCS_ROOTS)


def docs_only(raw_diff: str) -> list[str] | None:
    """Return the changed paths if EVERY diff record is a prose edit, else ``None`` (phaze-fqfds).

    PURE: takes the stdout of ``git diff --raw -M -z <base> <head>`` and no other input, so
    ``tests/shared/test_fast_gate.py`` exercises the shipped rule over synthetic records rather
    than restating it.

    ``--raw`` rather than ``--name-only`` because the modes are half the classification: an empty
    ``--name-only`` line cannot tell a prose edit from ``chmod +x``, a file replaced by a symlink,
    or a submodule pointer moved. Each record is ``:<srcmode> <dstmode> <srcsha> <dstsha>
    <status>`` followed by one path, or two for a rename or a copy -- and for a rename BOTH ends
    must be prose, so moving a file out of ``docs/`` into ``src/`` is a code change.

    ``None`` is returned for anything that does not qualify AND for anything that cannot be parsed
    -- a merge's combined record, a truncated stream, an unrecognised status. It is also returned
    for an EMPTY diff: "no paths changed" is not "all changed paths are prose", and the caller's
    normal path still runs the always-run floor. Fail closed in every direction.
    """
    tokens = [t for t in raw_diff.split("\0") if t]
    if not tokens:
        return None

    paths: list[str] = []
    index = 0
    while index < len(tokens):
        header = tokens[index]
        # `::` is a combined (merge) record, which carries a different field count entirely.
        if not header.startswith(":") or header.startswith("::"):
            return None
        fields = header[1:].split()
        if len(fields) != 5:
            return None
        src_mode, dst_mode, _src_sha, _dst_sha, status = fields
        letter = status[:1]
        if letter not in CLASSIFIABLE_STATUSES:
            return None
        expected_paths = 2 if letter in TWO_PATH_STATUSES else 1
        record_paths = tokens[index + 1 : index + 1 + expected_paths]
        if len(record_paths) != expected_paths:
            return None
        index += 1 + expected_paths

        if any(mode not in {DOCS_MODE, EMPTY_MODE} for mode in (src_mode, dst_mode)):
            return None
        if not all(is_docs_path(candidate) for candidate in record_paths):
            return None
        paths.extend(record_paths)

    return sorted(set(paths))


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


def prose_paths(repo: Path, base_ref: str) -> list[str] | None:
    """Return the changed paths if this diff is documentation-only, else ``None`` (phaze-fqfds).

    Called BEFORE :func:`select` and doing its own :func:`assert_clean_worktree` first, and both
    halves of that sentence are load-bearing:

    * **Before** ``select``, because ``select`` refuses on ``no_index`` -- the DEFAULT state in a bh
      worktree, since the coverage map is keyed by repo path and lives in the main clone. Asking
      repowise first means every prose diff escalates on a condition that has nothing to do with
      it, which is exactly the ~21 minutes this bead exists to remove.
    * **After the dirty-tree refusal**, because this reads a COMMITTED range (failure mode F) and
      cannot see an uncommitted ``.py`` edit. Deciding "prose only" over a dirty tree would weaken
      the gate against code it never looked at.
    """
    assert_clean_worktree(repo)
    head = _git(repo, "rev-parse", "HEAD")
    base = _git(repo, "merge-base", base_ref, "HEAD")
    return docs_only(_git(repo, "diff", "--raw", "-M", "-z", base, head, strip=False))


def select(repo: Path, base_ref: str) -> tuple[list[str], list[str]]:
    """Return ``(node_ids, notes)`` for the change, or raise ``Refuse``."""
    notes: list[str] = []
    assert_clean_worktree(repo)

    head = _git(repo, "rev-parse", "HEAD")
    base = _git(repo, "merge-base", base_ref, "HEAD")
    notes.append(f"range: {base[:8]}..{head[:8]} (vs {base_ref})")

    main_clone = Path(_git(repo, "rev-parse", "--git-common-dir")).resolve().parent
    if not (main_clone / ".repowise").is_dir():
        raise Refuse("escalate", f"no repowise index at {main_clone} — run `repowise init` there")
    notes.append(f"index: {main_clone}")

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
    parser.add_argument("--docs-floor", default="tests/docs_floor.txt", help="the prose floor: what a documentation-only diff runs")
    parser.add_argument("--out", required=True, help="file to write the pytest arguments to, one per line")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        floor = read_floor(repo / args.floor)
        docs_floor = read_floor(repo / args.docs_floor)
        prose = prose_paths(repo, args.base)
        if prose is not None:
            # A verdict of its own rather than a `run` over a swapped floor: the caller has to be
            # able to say in its transcript that the SUITE did not run, and a `run 11` line reads
            # like any other small selection. `read_floor` has already refused an empty manifest,
            # so this can never be the zero-test path by accident.
            Path(args.out).write_text("\n".join(docs_floor) + "\n", encoding="utf-8")
            shown = ", ".join(prose[:5]) + (" \u2026" if len(prose) > 5 else "")
            print(f"docs {len(docs_floor)} prose-guard module(s) for {len(prose)} documentation path(s): {shown}")  # noqa: T201
            return VERDICT_EXIT["docs"]
        node_ids, notes = select(repo, args.base)
    except Refuse as refusal:
        print(f"{refusal.verdict} {refusal.reason}")  # noqa: T201
        return VERDICT_EXIT[refusal.verdict]

    for note in notes:
        print(f"  {note}", file=sys.stderr)  # noqa: T201

    # The floor goes FIRST so that a truncated transcript still shows the gate ran something real.
    selection = floor + [n for n in node_ids if n not in set(floor)]
    Path(args.out).write_text("\n".join(selection) + "\n", encoding="utf-8")
    print(f"run {len(selection)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
