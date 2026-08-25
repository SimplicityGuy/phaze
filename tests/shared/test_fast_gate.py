"""Guard over the change-driven fast gate: the floor, the recipes, and the refusal rules (phaze-pv3kk).

``bh work check`` / ``submit`` / ``merge`` / ``merge-main`` run ``just check-fast``, which selects
tests from the bead's own diff via ``repowise impacted-tests`` (operator decision 2026-08-25). Only
``molecule`` still runs the full suite. A fast gate is therefore the LAST thing before ``main`` for an
ad-hoc bead, and post-push CI is the backstop.

That is the whole reason this file is mostly about REFUSING. ``repowise impacted-tests`` reports
every degraded state -- no index, empty map, unknown files, inference instead of coverage -- as a
warning on stderr and **exit 0**. Each one, piped naively into pytest, runs zero tests and reports
green. ``scripts/select_impacted_tests.py`` turns each into a verdict; the tests below exercise the
SHIPPED classifier over synthetic reports rather than restating its rules, so the guard cannot drift
from the thing it guards. Same precedent as ``tests/shared/test_coverage_floor.py``: load the real
script from its path and call its real function.

phaze-fqfds added a FOURTH verdict, ``docs``: a diff whose every path is prose runs
``tests/docs_floor.txt`` -- every test module that reads tracked prose, 193 tests in 9.4-9.9 s -- and
not the suite. The operator's answer there was PERMISSION rather than a mandate ("for docs only
changes, no tests are needed!", clarified as "if tests run, ok; if they don't also fine"), so WHAT
runs is an engineering choice and these tests are what hold it.

Two sections below carry that weight. The ALLOW-LIST section pins that a mixed diff, a
``justfile``/``pyproject.toml``/YAML/template diff, a doc-extension file inside a shipped tree, a
chmod, a symlink and an empty diff all refuse the docs path -- widening the allow-list without
moving them fails the build. The PROSE FLOOR section pins the manifest by DERIVATION rather than by
name: it scans every test module for a reference to a tracked prose path and fails when one is
missing from the floor, which is what stops the floor decaying into a curated three that announces
more coverage than it has.

WHAT THIS FILE CANNOT REACH, and it is more than usual here. Which command each beadhive boundary
runs lives in ``~/.beadhive/config.yaml`` -- machine-wide, outside this repo. CLAUDE.md already
records that ``test_validation_gate_recipes.py`` guards the justfile half and nothing can guard the
config half. Under the 2026-08-25 boundary decision that gap widened: ``merge`` and ``merge-main``
must resolve to the FAST command and ``molecule`` must keep ``just check-all``, and if the
``molecule`` override is ever lost it silently inherits the fast command -- at which point NOTHING
runs the full suite before ``main``, and every gate still prints green. Only reading that file, or
watching a real merge transcript, establishes it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
FLOOR_PATH = TESTS_ROOT / "fast_floor.txt"
SELECTOR_PATH = REPO_ROOT / "scripts" / "select_impacted_tests.py"
BUCKETS_PATH = TESTS_ROOT / "buckets.json"

EXCLUDED_BUCKET = "browser"

# The floor's two admissible reasons, pinned by name because neither is visible to a scoring
# function. (1) meta-guards over invariants CLAUDE.md documents -- including the gate wiring this
# bead itself changed, which the superseded design's optimiser dropped; (2) the app's own boot path,
# which a change-selected run can miss entirely while the application no longer starts.
REQUIRED_IN_FLOOR: frozenset[str] = frozenset(
    {
        # (1) meta-guards
        "tests/analyze/services/pipeline/test_analysis_streaming_decode.py",
        "tests/shared/test_coverage_floor.py",
        "tests/shared/test_db_guard.py",
        "tests/shared/test_partition_guard.py",
        "tests/shared/test_repowise_coverage_gate.py",
        "tests/shared/test_test_db_session_exclusivity.py",
        "tests/shared/test_validation_gate_recipes.py",
        # This file. A fast gate that does not run its own guard is not guarded -- and under a
        # change-driven selector `impacted-tests` would select it only on beads that touch it, so a
        # rail-guard left out of the floor runs precisely when it is least needed.
        #
        # DISPATCHER'S DECISION, 2026-08-25, bead phaze-pv3kk -- NOT the operator's. Two acceptance
        # criteria on that bead conflict: "a subset of EXISTING tests, adds no new ones. A diff
        # showing new test functions fails this criterion" against "a guard in tests/shared/ fails
        # the build when the fast set decays". The operator's own words govern the first --
        # "it should not be new tests, rather it should be a subset of existing tests" -- and they
        # are about what the gate runs TO CHECK PHAZE, i.e. the coverage. The guard requirement was
        # authored by the dispatcher, not the operator, so resolving the conflict in favour of
        # keeping this file in the always-run set is the dispatcher's call to make and is recorded
        # as theirs. The operator can override it.
        "tests/shared/test_fast_gate.py",
        # (2) the app's own boot path
        "tests/shared/core/test_health.py",
        "tests/shared/core/test_main_lifespan.py",
        "tests/shared/core/test_route_reachability.py",
    }
)

# The floor runs on EVERY gate invocation, on top of whatever the change selects. Its job is a
# non-empty guarantee plus boot smoke -- NOT breadth, which is now the selector's job. This cap
# exists because the superseded design of this bead was a 348-module static subset, and the failure
# mode is that the floor quietly grows back into it one "surely this one too" at a time.
# Measured 2026-08-25 on worktree seat `pv3kk`: 35 modules, ~36 s.
MAX_FLOOR_MODULES = 45

DOCS_FLOOR_PATH = TESTS_ROOT / "docs_floor.txt"

# The prose floor's load-bearing members, pinned by name on top of the derivation scan below. The
# scan keeps the floor from UNDER-covering as new guards land; these three keep it from being
# emptied out, because they are the ones with an incident behind them: phaze-f70y9 (bare ADR
# numbers resolving to the wrong document after a renumber) and ADR-0012's attribution form, whose
# guard fired for real on a seat's draft during the 2026-08-25 wave.
REQUIRED_IN_DOCS_FLOOR: frozenset[str] = frozenset(
    {
        "tests/shared/test_adr_citation_resolution.py",
        "tests/shared/test_adr_numbering.py",
        "tests/shared/test_operator_attribution_citations.py",
    }
)

# The floor exists to be fast; past this it is just the suite with extra steps. Measured
# 2026-08-25 on seat `docsgate`: 11 modules, 193 tests, 9.4-9.9 s.
MAX_DOCS_FLOOR_MODULES = 20

# What "reads tracked prose" looks like in a test module. Deliberately BROAD: a false positive
# costs milliseconds in the floor, a false negative is a guard that never runs on the only gate a
# docs bead traverses. Written as literals a module would have to contain to touch prose at all.
PROSE_PATH_MARKERS = ('"docs/', '"docs"', '"CLAUDE.md"', '"README.md"', '"CONVENTIONS.md"', '".planning', '"design/')


def _load_selector() -> ModuleType:
    """Import the real ``scripts/select_impacted_tests.py`` so the classifier under test is shipped code."""
    assert SELECTOR_PATH.is_file(), f"selector script missing: {SELECTOR_PATH}"
    spec = importlib.util.spec_from_file_location("select_impacted_tests", SELECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SELECTOR = _load_selector()


def _read_floor() -> list[str]:
    lines = FLOOR_PATH.read_text(encoding="utf-8").splitlines()
    return [stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")]


def _report(**overrides: Any) -> dict[str, Any]:
    """A well-formed ``impacted-tests`` report: one covered test, nothing degraded."""
    base: dict[str, Any] = {
        "diff": "a..b",
        "changed_files": 1,
        "no_index": False,
        "map_empty": False,
        "impacted_tests": [
            {
                "test_id": "tests/review/routers/test_proposals.py::test_approve_proposal|run",
                "test_file": "tests/review/routers/test_proposals.py",
                "source_files": ["src/phaze/routers/proposals.py"],
                "via": "coverage",
            }
        ],
        "inferred_tests": [],
        "unknown_files": [],
    }
    base.update(overrides)
    return base


def _refusal(report: dict[str, Any]) -> tuple[str, str]:
    """Run the shipped classifier and return ``(verdict, reason)``, failing if it did not refuse."""
    with pytest.raises(SELECTOR.Refuse) as excinfo:
        SELECTOR.classify(report)
    return excinfo.value.verdict, excinfo.value.reason


# --------------------------------------------------------------------------------------------
# The refusal rules. Each of these is a state repowise reports with exit 0.
# --------------------------------------------------------------------------------------------


def test_a_clean_report_selects_the_covered_tests_with_the_context_suffix_stripped() -> None:
    """The happy path, and the ``|run`` strip that makes it usable at all.

    The map stores coverage CONTEXTS, so ids arrive as ``…::test_x|run``. Measured 2026-08-25:
    pytest exits 4 (usage error) on that form. It fails loudly rather than silently, which is the
    good direction -- but it means repowise's own documented ``--format list | xargs pytest`` does
    not work for this tier, and the strip is not optional.
    """
    node_ids, covered_sources = SELECTOR.classify(_report())
    assert node_ids == ["tests/review/routers/test_proposals.py::test_approve_proposal"]
    assert covered_sources == {"src/phaze/routers/proposals.py"}


def test_a_changed_test_file_is_run_rather_than_escalated() -> None:
    """``changed-test`` is the one inference tier that is not a claim about coverage.

    It says "the changed file IS a test file", which is a fact about the diff and needs no map. The
    other three tiers are candidates and escalate; conflating them would either escalate on every
    test-only change (useless) or run name-shaped guesses as though measured (unsound).
    """
    report = _report(
        impacted_tests=[],
        inferred_tests=[{"source_file": "tests/shared/test_x.py", "test_file": "tests/shared/test_x.py", "via": "changed-test"}],
    )
    node_ids, covered_sources = SELECTOR.classify(report)
    assert node_ids == ["tests/shared/test_x.py"]
    assert covered_sources == set()


@pytest.mark.parametrize("via", ["call-graph", "import-graph", "filename-pattern"])
def test_inference_that_claims_coverage_escalates(via: str) -> None:
    """repowise: "All are file-level and all over-claim; none may be read as coverage."."""
    report = _report(inferred_tests=[{"source_file": "src/phaze/x.py", "test_file": "tests/test_x.py", "via": via}])
    verdict, reason = _refusal(report)
    assert verdict == "escalate"
    assert via in reason


def test_an_unrecognised_inference_label_escalates_rather_than_being_assumed_safe() -> None:
    """A ``via`` this script has never seen is a repowise upgrade, not a tier to trust by default."""
    report = _report(inferred_tests=[{"source_file": "src/phaze/x.py", "test_file": "tests/test_x.py", "via": "brand-new-tier"}])
    verdict, reason = _refusal(report)
    assert verdict == "escalate"
    assert "brand-new-tier" in reason


def test_unknown_files_escalate_because_that_is_the_normal_case_here() -> None:
    """Templates, pyproject and the justfile have no coverage rows at all.

    phaze renders its whole UI from Jinja, and the map holds nothing for ``.html``. Measured on a
    sampled range: 6 unknown files, two of them templates. A change to a template selects ZERO
    tests, so without this rule the gate would be green having exercised none of the change.
    repowise's own docstring for this tier: "Nothing said anything. Run the full suite."
    """
    verdict, reason = _refusal(_report(unknown_files=["src/phaze/templates/pipeline/partials/_diff_row.html"]))
    assert verdict == "escalate"
    assert "_diff_row.html" in reason


@pytest.mark.parametrize("flag", ["no_index", "map_empty"])
def test_a_missing_or_empty_map_escalates(flag: str) -> None:
    """Both are reported by repowise with exit 0 and an empty selection.

    ``no_index`` is the DEFAULT state in a bh worktree -- the map is keyed by repo path and lives in
    the main clone -- so without this rule every bead's gate would run zero tests and pass.
    """
    verdict, _ = _refusal(_report(**{flag: True}))
    assert verdict == "escalate"


def test_a_changed_diff_that_selects_nothing_is_a_hard_failure_not_a_pass() -> None:
    """ "Exit 0 having measured nothing" is the phaze-jnj90 defect; it may not be reachable here."""
    verdict, reason = _refusal(_report(impacted_tests=[], inferred_tests=[], changed_files=3))
    assert verdict == "fail"
    assert "measured nothing" in reason


def test_an_empty_diff_selects_nothing_without_failing() -> None:
    """A no-op diff is legitimate: the floor still runs, so the gate never executes zero tests."""
    node_ids, _ = SELECTOR.classify(_report(impacted_tests=[], changed_files=0))
    assert node_ids == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tests/a.py::test_b|run", "tests/a.py::test_b"),
        ("tests/a.py::test_b|setup", "tests/a.py::test_b"),
        ("tests/a.py::test_b|teardown", "tests/a.py::test_b"),
        ("tests/a.py::test_b", "tests/a.py::test_b"),
        ("tests/a.py::test_b|unheard-of", "tests/a.py::test_b|unheard-of"),
    ],
)
def test_strip_context_only_strips_known_phases(raw: str, expected: str) -> None:
    """An unknown suffix is left alone so pytest rejects it loudly rather than being mangled quietly."""
    assert SELECTOR.strip_context(raw) == expected


# --------------------------------------------------------------------------------------------
# The docs allow-list (phaze-fqfds). A skip runs ZERO tests, so every assertion here is about
# something REFUSING to skip. `docs_only` and `is_docs_path` are pure, so these exercise the
# shipped functions over synthetic `git diff --raw -z` records -- the same precedent as the
# refusal rules above: load the real script, call its real function, never restate the rule.
# --------------------------------------------------------------------------------------------


def _raw(*records: tuple[str, str, str] | tuple[str, str, str, str]) -> str:
    """Build ``git diff --raw -M -z`` stdout from ``(src_mode, dst_mode, status, *paths)`` records.

    The real thing, byte for byte: ``:<srcmode> <dstmode> <srcsha> <dstsha> <status>`` then the
    path(s), every field NUL-terminated. Written out rather than captured from a fixture repo so a
    reader can see exactly which field each assertion below is aimed at.
    """
    out = ""
    for record in records:
        src_mode, dst_mode, status, *paths = record
        out += f":{src_mode} {dst_mode} 1111111 2222222 {status}\0" + "".join(f"{p}\0" for p in paths)
    return out


def _modified(*paths: str) -> str:
    """The ordinary case: each path modified in place, regular non-executable blob."""
    return _raw(*[("100644", "100644", "M", p) for p in paths])


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md",
        "README.md",
        "CONVENTIONS.md",
        "docs/design/0012-verification-fidelity-and-operator-attribution.md",
        "docs/spikes/phaze-u1n7j-vox-fix-verification.md",
        ".planning/STATE.md",
        "design/DESIGN_SYSTEM.md",
    ],
)
def test_prose_paths_are_documentation(path: str) -> None:
    """The population this bead exists for: root-level Markdown plus the three prose trees."""
    assert SELECTOR.is_docs_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        # Outside the coverage map and fully executable -- the exact set a "not a .py file"
        # negation would have handed the skip to.
        "justfile",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        ".pre-commit-config.yaml",
        "src/phaze/templates/pipeline/partials/_diff_row.html",
        "scripts/provision-test-seat.sh",
        "src/phaze/main.py",
        # Doc EXTENSIONS that are not `.md`. `docs/` holds .py benchmarks, .sh drivers, .patch
        # files and .html prototypes, so "everything under docs/" is not a safe rule.
        "docs/spikes/phaze-han03/bench_decode.py",
        "docs/spikes/phaze-0ni3v/make-corpus.sh",
        "docs/spikes/phaze-bk9el.1-health-baseline-2026-08-21.json",
        "docs/ui-reference-fixtures.html",
        "docs/notes.txt",
        "LICENSE",
        # phaze-tlo10, stated positively: a doc-extension file inside a shipped tree is code.
        # naming.md is loaded at runtime by load_prompt_template() and depended on for its exact
        # placeholder lines; classifying it as prose let a prompt-only PR skip CI entirely.
        "src/phaze/prompts/naming.md",
        "src/phaze/agent_watcher/README.md",
        "tests/identify/fixtures/tracklist_render/README.md",
        "tests/BUCKETS.md",
        "scripts/some-notes.md",
        "services/notes.md",
        # A new prose directory is not assumed prose; it escalates until someone adds it here.
        "notes/whatever.md",
        # Degenerate shapes.
        "",
        ".md",
        "docs/../src/phaze/prompts/naming.md",
    ],
)
def test_everything_else_is_not_documentation(path: str) -> None:
    """The allow-list is positive, so this is the assertion that it is not vacuously permissive."""
    assert SELECTOR.is_docs_path(path) is False


def test_a_prose_only_diff_skips() -> None:
    """The whole point: the six beads measured on 2026-08-25 paid ~21 minutes each for this shape."""
    assert SELECTOR.docs_only(_modified("CLAUDE.md", "docs/design/0007-windowed-analysis.md")) == [
        "CLAUDE.md",
        "docs/design/0007-windowed-analysis.md",
    ]


def test_one_python_file_among_twenty_markdown_ones_does_not_skip() -> None:
    """`every`, not `any`. This is the security property, so it is not a table row.

    A code change riding a docs skip past the last gate before `main` is the phaze-jnj90 defect
    with a new door: the gate exits 0 having measured nothing, and for an ad-hoc bead nothing
    downstream runs the suite either.
    """
    paths = [f"docs/design/{i:04d}-note.md" for i in range(20)]
    assert SELECTOR.docs_only(_modified(*paths, "src/phaze/services/analysis.py")) is None


@pytest.mark.parametrize(
    "path",
    ["justfile", "pyproject.toml", ".github/workflows/ci.yml", ".pre-commit-config.yaml", "src/phaze/templates/pipeline/index.html", "scripts/x.sh"],
)
def test_a_build_affecting_diff_does_not_skip_even_with_no_python_in_it(path: str) -> None:
    """One category per acceptance criterion: none of these has a coverage row either.

    They are what makes "the coverage map cannot speak for this file" the wrong test for a skip:
    every one of them is unmeasurable AND executable, so the map's silence means the opposite of
    what it means for prose.
    """
    assert SELECTOR.docs_only(_modified(path)) is None
    assert SELECTOR.docs_only(_modified("CLAUDE.md", path)) is None


def test_a_deleted_markdown_file_still_skips() -> None:
    """Deleting prose is a prose edit; the destination mode is empty, not a mode to reject."""
    assert SELECTOR.docs_only(_raw(("100644", "000000", "D", "docs/spikes/old.md"))) == ["docs/spikes/old.md"]


def test_a_rename_must_be_prose_at_BOTH_ends() -> None:
    """Renaming a file out of a prose tree into a shipped one is a code change, not a move."""
    assert SELECTOR.docs_only(_raw(("100644", "100644", "R100", "docs/a.md", "docs/b.md"))) == ["docs/a.md", "docs/b.md"]
    assert SELECTOR.docs_only(_raw(("100644", "100644", "R100", "docs/a.md", "src/phaze/prompts/a.md"))) is None
    assert SELECTOR.docs_only(_raw(("100644", "100644", "R100", "src/phaze/prompts/a.md", "docs/a.md"))) is None


@pytest.mark.parametrize(
    ("src_mode", "dst_mode", "why"),
    [
        ("100644", "100755", "chmod +x on a Markdown file"),
        ("100755", "100755", "an already-executable Markdown file"),
        ("100644", "120000", "a Markdown file replaced by a symlink"),
        ("120000", "100644", "a symlink replaced by a Markdown file"),
        ("160000", "160000", "a submodule pointer whose path happens to end in .md"),
    ],
)
def test_a_mode_change_does_not_skip(src_mode: str, dst_mode: str, why: str) -> None:
    """The reason this reads `--raw` and not `--name-only`: none of these is visible in a path.

    `git diff --name-only` prints `docs/x.md` for every row here, so a classifier built on it
    would skip a chmod that made a tracked file executable.
    """
    assert SELECTOR.docs_only(_raw((src_mode, dst_mode, "M", "docs/x.md"))) is None, why


@pytest.mark.parametrize(
    "status",
    ["T", "U", "X", "Z"],
)
def test_an_unclassifiable_status_does_not_skip(status: str) -> None:
    """Fail closed on a status this classifier was not written against, including a future one."""
    assert SELECTOR.docs_only(_raw(("100644", "100644", status, "docs/x.md"))) is None


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("", "an empty diff is not 'all changed paths are prose'"),
        ("\0\0", "nothing but separators"),
        ("docs/x.md\0", "a bare path with no record header — `--name-only` output fed in by mistake"),
        ("::100644 100644 100644 1111111 2222222 3333333 MM\0docs/x.md\0", "a combined (merge) record"),
        (":100644 100644 1111111 M\0docs/x.md\0", "a header with too few fields"),
        (":100644 100644 1111111 2222222 M\0", "a record whose path is missing"),
        (":100644 100644 1111111 2222222 R100\0docs/a.md\0", "a rename missing its second path"),
    ],
)
def test_anything_unparseable_does_not_skip(raw: str, why: str) -> None:
    """The classifier cannot decide, so it does not. Every unknown shape falls to the normal path.

    An empty diff is in here on purpose: it is legitimate (a no-op range), and the normal path
    already handles it by running the always-run floor. Reading it as a skip would turn a broken
    diff base into a green gate that ran nothing.
    """
    assert SELECTOR.docs_only(raw) is None, why


def test_the_docs_path_is_reachable_only_after_the_dirty_tree_refusal() -> None:
    """Order is the guardrail, not a detail (phaze-fqfds).

    `prose_paths` reads a COMMITTED range, which cannot see an uncommitted `.py` edit -- failure
    mode F. If it ran before `assert_clean_worktree`, a seat with staged code and a committed prose
    diff would take the narrowed path over code the gate never looked at. It is not callable here
    without a real repo, so this asserts the source order, which is what actually fixes it.
    """
    source = SELECTOR_PATH.read_text(encoding="utf-8")
    body = source.split("def prose_paths(", 1)[1].split("\ndef ", 1)[0]
    assert body.index("assert_clean_worktree(repo)") < body.index("docs_only("), "prose_paths() classifies the diff before refusing a dirty worktree"
    # And main() must ask prose_paths BEFORE select(), or a prose diff escalates on `no_index` --
    # the default state in a bh worktree, and the exact cost this bead exists to remove.
    main_body = source.split("def main(", 1)[1]
    assert main_body.index("prose_paths(") < main_body.index("select(repo, args.base)"), (
        "main() calls select() before prose_paths(): every prose diff would escalate on `no_index`"
    )


def test_the_docs_verdict_is_exit_4_and_not_exit_2() -> None:
    """argparse exits 2 on a usage error, so 2 must never mean "do not run the suite"."""
    assert SELECTOR.VERDICT_EXIT == {"escalate": 3, "fail": 1, "docs": 4}


def test_the_allow_list_constants_are_the_shipped_narrow_ones() -> None:
    """Same job as `test_the_classifier_under_test_is_the_shipped_one`, for the new rule.

    Widening any of these is a decision to argue on a bead; it is not something a later edit gets
    to do quietly, because this assertion fails the build when it happens.
    """
    assert SELECTOR.DOCS_SUFFIX == ".md"
    assert SELECTOR.DOCS_ROOTS == ("docs/", ".planning/", "design/")
    assert SELECTOR.DOCS_MODE == "100644"
    assert sorted(SELECTOR.CLASSIFIABLE_STATUSES) == ["A", "C", "D", "M", "R"]
    assert sorted(SELECTOR.TWO_PATH_STATUSES) == ["C", "R"]


def test_the_ci_classifier_is_deliberately_a_different_and_looser_rule() -> None:
    """These two must not be merged, and the difference is measurable rather than stylistic.

    `scripts/classify-changed-files.sh` decides whether CI runs its heavy jobs and treats ALL of
    `docs/` as prose whatever the extension, plus `*.txt` and `LICENSE`. This gate is the last
    thing before `main` for an ad-hoc bead and is stricter. Sharing one implementation would let a
    widening of the CI rule silently widen this one.
    """
    for path in ("docs/spikes/phaze-han03/bench_decode.py", "docs/notes.txt", "LICENSE"):
        assert SELECTOR.is_docs_path(path) is False, f"{path} must not be prose to this gate"


def test_the_fast_step_honours_the_docs_verdict() -> None:
    """The recipe half. A verdict the justfile drops on the floor changes nothing.

    Exit 4 would otherwise fall into the recipe's `*)` arm and fail the gate, so this asserts the
    branch exists, that it passes the prose floor to the selector, that it actually RUNS the
    selection, and that it announces itself -- a narrowed gate that prints nothing is
    indistinguishable from one that broke.
    """
    recipe = _dry_run("test-fast")
    assert "docs_floor.txt" in recipe, "`just test-fast` no longer passes the prose floor to the selector"
    assert "DOCS-ONLY" in recipe, "`just test-fast` no longer announces the docs path"
    assert "fast-gate-docs-runs.log" in recipe, "`just test-fast` no longer records when it narrowed itself"
    docs_arm = recipe.split("DOCS-ONLY", 1)[1]
    assert "uv run pytest" in docs_arm, "the docs arm no longer runs the prose floor — it would report green over zero tests"
    assert "ensure_seat" in docs_arm, (
        "the docs arm no longer provisions a seat, so its pytest header reads `unlocked` — the state CLAUDE.md tells readers not to trust"
    )


# --------------------------------------------------------------------------------------------
# The floor.
# --------------------------------------------------------------------------------------------


def test_the_floor_is_non_empty_and_every_path_exists() -> None:
    """The floor IS the non-empty guarantee, so an empty or dangling floor defeats its only job."""
    floor = _read_floor()
    assert floor, "tests/fast_floor.txt names nothing — the gate could run zero tests"
    missing = [p for p in floor if not (REPO_ROOT / p).is_file()]
    assert not missing, f"tests/fast_floor.txt names {len(missing)} path(s) that do not exist: {missing}"
    outside = [p for p in floor if not p.startswith("tests/")]
    assert not outside, f"tests/fast_floor.txt names path(s) outside tests/: {outside}"
    browser = [p for p in floor if Path(p).parts[1] == EXCLUDED_BUCKET]
    assert not browser, f"the browser bucket is deselected by pyproject's addopts and cannot run here: {browser}"


def test_the_floor_is_sorted_and_free_of_duplicates() -> None:
    """A duplicate runs a module twice; an unsorted file makes every diff unreadable."""
    floor = _read_floor()
    duplicates = sorted({p for p in floor if floor.count(p) > 1})
    assert not duplicates, f"tests/fast_floor.txt lists these more than once: {duplicates}"
    assert floor == sorted(floor), "tests/fast_floor.txt must be sorted"


def test_the_floor_carries_the_meta_guards_and_the_boot_path() -> None:
    """Both are invisible to a scoring function, so both are pinned by name.

    The evidence that this is needed is this bead's own history: the superseded design's optimiser
    dropped ``test_validation_gate_recipes.py`` -- the guard over the recipes it was changing.
    """
    missing = sorted(REQUIRED_IN_FLOOR - set(_read_floor()))
    assert not missing, (
        f"these are missing from tests/fast_floor.txt: {missing}. They are there because an optimiser "
        "cannot see criticality or startup; removing one is an argument to make on the bead."
    )


def test_the_floor_stays_a_floor() -> None:
    """It runs on every gate forever, and the superseded design is what it would grow back into."""
    floor = _read_floor()
    assert len(floor) <= MAX_FLOOR_MODULES, (
        f"tests/fast_floor.txt names {len(floor)} modules, over the {MAX_FLOOR_MODULES} cap. The floor is a "
        "non-empty guarantee plus boot smoke, NOT breadth — breadth is the selector's job. If a module "
        "genuinely belongs, re-measure and move the cap and its quoted measurement together."
    )


def test_every_floor_module_lives_in_a_known_bucket() -> None:
    """Same single source of truth the CI matrix and test_partition_guard.py consume."""
    buckets = set(json.loads(BUCKETS_PATH.read_text(encoding="utf-8")))
    strays = sorted({p for p in _read_floor() if Path(p).parts[1] not in buckets})
    assert not strays, f"floor modules outside any tests/buckets.json bucket: {strays}"


# --------------------------------------------------------------------------------------------
# The prose floor (phaze-fqfds). What a documentation-only diff runs, and all it runs.
# --------------------------------------------------------------------------------------------


def _read_docs_floor() -> list[str]:
    lines = DOCS_FLOOR_PATH.read_text(encoding="utf-8").splitlines()
    return [stripped for line in lines if (stripped := line.strip()) and not stripped.startswith("#")]


def test_the_prose_floor_is_non_empty_and_every_path_exists() -> None:
    """An empty or dangling prose floor turns the docs verdict into a zero-test green gate."""
    floor = _read_docs_floor()
    assert floor, "tests/docs_floor.txt names nothing — a docs-only diff would run zero tests"
    missing = [p for p in floor if not (REPO_ROOT / p).is_file()]
    assert not missing, f"tests/docs_floor.txt names {len(missing)} path(s) that do not exist: {missing}"
    outside = [p for p in floor if not p.startswith("tests/")]
    assert not outside, f"tests/docs_floor.txt names path(s) outside tests/: {outside}"
    browser = [p for p in floor if Path(p).parts[1] == EXCLUDED_BUCKET]
    assert not browser, f"the browser bucket is deselected by pyproject's addopts and cannot run here: {browser}"


def test_the_prose_floor_is_sorted_and_free_of_duplicates() -> None:
    """A duplicate runs a module twice; an unsorted file makes every diff unreadable."""
    floor = _read_docs_floor()
    duplicates = sorted({p for p in floor if floor.count(p) > 1})
    assert not duplicates, f"tests/docs_floor.txt lists these more than once: {duplicates}"
    assert floor == sorted(floor), "tests/docs_floor.txt must be sorted"


def test_every_prose_floor_module_lives_in_a_known_bucket() -> None:
    """Same single source of truth the CI matrix and test_partition_guard.py consume."""
    buckets = set(json.loads(BUCKETS_PATH.read_text(encoding="utf-8")))
    strays = sorted({p for p in _read_docs_floor() if Path(p).parts[1] not in buckets})
    assert not strays, f"prose floor modules outside any tests/buckets.json bucket: {strays}"


def test_the_prose_floor_keeps_the_guards_with_an_incident_behind_them() -> None:
    """The derivation scan stops the floor UNDER-covering; this stops it being emptied.

    test_adr_citation_resolution.py exists because of phaze-f70y9 -- eight bare "ADR-0014"
    citations that came to resolve to the wrong document after a renumber, caught once by a human
    reading prose and by nothing else. Removing one of these is an argument to make on a bead.
    """
    missing = sorted(REQUIRED_IN_DOCS_FLOOR - set(_read_docs_floor()))
    assert not missing, f"these are missing from tests/docs_floor.txt: {missing}"


def test_the_prose_floor_stays_small() -> None:
    """It is chosen over the suite for its runtime; past this cap that argument stops holding."""
    floor = _read_docs_floor()
    assert len(floor) <= MAX_DOCS_FLOOR_MODULES, (
        f"tests/docs_floor.txt names {len(floor)} modules, over the {MAX_DOCS_FLOOR_MODULES} cap. "
        "If they genuinely all read prose, re-measure the runtime and move the cap and its quoted "
        "measurement together — the cap is only meaningful next to a number."
    )


def test_every_test_module_that_reads_prose_is_in_the_prose_floor() -> None:
    """THE anti-drift guard, and the reason the floor can be trusted to mean what it says.

    Without this the floor is a curated list, and a curated list decays in the one direction that
    matters: a prose guard written next month never joins it, a docs bead's gate sails past the
    break, and the transcript still announces that the prose guards ran. A gate whose output
    overstates its own coverage is the phaze-jnj90 / phaze-nqawu family.

    The scan OVER-matches on purpose. A module that merely mentions ``docs/`` in a docstring costs
    a few milliseconds in the floor; one that reads prose and is absent from it is a silent hole.
    So the fix when this fails is nearly always to add the line, not to narrow the markers.
    """
    floor = set(_read_docs_floor())
    referencing: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if Path(relative).parts[1] == EXCLUDED_BUCKET:
            continue
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in PROSE_PATH_MARKERS):
            referencing.append(relative)

    assert referencing, "the prose scan matched nothing — its markers have stopped matching anything real"
    missing = sorted(set(referencing) - floor)
    assert not missing, (
        f"{len(missing)} test module(s) reference a tracked prose path but are absent from "
        f"tests/docs_floor.txt, so a documentation-only diff would not run them: {missing}. "
        "Add them to the floor."
    )


def _throwaway_repo(root: Path, *changed: str) -> str:
    """A real git repo with a real base commit and a real second commit touching ``changed``.

    Small enough to build per test (~50 ms) and worth it: the alternative is asserting on the
    selector's SOURCE TEXT, which passes just as happily when the wiring is broken and fails when
    the formatter moves a line. This runs the shipped ``main()`` over a real diff.
    """
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)  # noqa: S603, S607
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    for relative in ("tests/fast_floor.txt", "tests/docs_floor.txt", *changed):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("tests/placeholder.py\n" if relative.endswith("floor.txt") else "base\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "--no-verify", "-m", "base"], check=True)  # noqa: S603
    base = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603
    for relative in changed:
        (root / relative).write_text("changed\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "--no-verify", "-m", "change"], check=True)  # noqa: S603
    return base


def _run_selector(root: Path, base: str, out: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped script exactly as the justfile does, plus an explicit ``--base``."""
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SELECTOR_PATH),
            "--repo",
            str(root),
            "--base",
            base,
            "--out",
            str(out),
            "--floor",
            "tests/fast_floor.txt",
            "--docs-floor",
            "tests/docs_floor.txt",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_a_prose_only_diff_writes_the_prose_floor_and_exits_4(tmp_path: Path) -> None:
    """End to end through the shipped ``main()``: verdict word, exit code, and the file it wrote.

    The exit code and the selection file are the entire contract with the justfile, so they are
    asserted against a real run rather than against the script's source text.
    """
    root = tmp_path / "repo"
    base = _throwaway_repo(root, "CLAUDE.md", "docs/design/0001-x.md")
    out = tmp_path / "selection.txt"
    result = _run_selector(root, base, out)

    assert result.returncode == SELECTOR.VERDICT_EXIT["docs"], result.stdout + result.stderr
    assert result.stdout.startswith("docs "), result.stdout
    assert out.read_text(encoding="utf-8").split() == ["tests/placeholder.py"], (
        "the docs verdict did not write the PROSE floor to --out, so the recipe would run a stale selection"
    )


def test_a_mixed_diff_never_reaches_exit_4(tmp_path: Path) -> None:
    """The same end-to-end path, proving the docs exit code is not simply always taken.

    One `.py` alongside the prose is a code change. It leaves the docs branch entirely and lands on
    the normal path, which in a repo with no repowise index escalates -- so the assertion that
    matters is `!= 4`, and the escalation is the incidental (correct) consequence.
    """
    root = tmp_path / "repo"
    base = _throwaway_repo(root, "CLAUDE.md", "src/phaze/thing.py")
    result = _run_selector(root, base, tmp_path / "selection.txt")

    assert result.returncode != SELECTOR.VERDICT_EXIT["docs"], f"a mixed diff took the docs path: {result.stdout}"
    assert result.returncode == SELECTOR.VERDICT_EXIT["escalate"], result.stdout + result.stderr


def test_a_dirty_worktree_never_reaches_exit_4(tmp_path: Path) -> None:
    """Fail closed. The docs check reads a COMMITTED range and cannot see the uncommitted `.py`.

    This is the ordering guardrail measured rather than read off the source: the prose diff IS
    committed and would classify as documentation, and the uncommitted code edit still stops it.
    """
    root = tmp_path / "repo"
    base = _throwaway_repo(root, "CLAUDE.md")
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "uncommitted.py").write_text("x = 1\n", encoding="utf-8")
    result = _run_selector(root, base, tmp_path / "selection.txt")

    assert result.returncode == SELECTOR.VERDICT_EXIT["fail"], result.stdout + result.stderr
    assert "uncommitted" in result.stdout


# --------------------------------------------------------------------------------------------
# The recipes. (The config half is unreachable from here — see the module docstring.)
# --------------------------------------------------------------------------------------------


def _dry_run(recipe: str) -> str:
    """Return what ``just`` would execute for ``recipe``, without executing it."""
    just = shutil.which("just")
    assert just is not None, "just is not on PATH"
    result = subprocess.run(  # noqa: S603 - fixed executable, recipe names are literals below
        [just, "--dry-run", recipe],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={"TEST_DATABASE_URL": "dry-run", "PATH": str(Path(just).parent) + ":/usr/bin:/bin"},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output


def test_check_fast_is_still_a_gate() -> None:
    """Dropping ruff or mypy would save ~26 s and cost the point of running it at all."""
    recipe = _dry_run("check-fast")
    assert "ruff check" in recipe, "`just check-fast` no longer runs ruff"
    assert "mypy" in recipe, "`just check-fast` no longer runs mypy"


def test_the_fast_step_runs_the_selector_and_can_escalate() -> None:
    """The selector and the escalation path are the gate; without either it is a fixed subset again."""
    recipe = _dry_run("test-fast")
    assert "select_impacted_tests.py" in recipe, "`just test-fast` no longer runs the change-driven selector"
    assert "fast_floor.txt" in recipe, "`just test-fast` no longer passes the always-run floor"
    assert "test-validate" in recipe, "`just test-fast` has no escalation path to the full suite"
    # An escalation that is quiet is a twenty-minute surprise nobody can explain, and a seat will
    # "optimise" it away. It must announce itself AND name its cause.
    assert "FAST-GATE-ESCALATION" in recipe, "`just test-fast` no longer announces an escalation"
    # And durably, because "is this gate escalating on most beads?" is a TREND question about how
    # stale the coverage map has become, and a per-run transcript cannot answer it.
    assert "fast-gate-escalations.log" in recipe, "`just test-fast` no longer records what escalated"


def test_the_fast_step_produces_no_coverage() -> None:
    """No coverage gate on the fast run -- operator decision 2026-08-25, durable record bead phaze-pv3kk.

    A subset's figure is meaningless against the 95% line floor and must not be quotable as one; and
    a ``coverage.json`` written here would overwrite the artifact ``just branch-check`` and
    ``scripts/coverage_floor.py`` compare against. repowise is unaffected either way: its coverage
    comes from ``just repowise-coverage`` and ``just repowise-coverage-ci``, so this bead READS the
    map and writes nothing to it.
    """
    recipe = _dry_run("test-fast")
    assert "--cov" not in recipe, "`just test-fast` emits coverage: a subset's number is not a floor and must not look like one"
    assert "coverage_floor.py" not in recipe, "`just test-fast` must not run the line-floor script over a subset"


def test_the_full_gate_is_unchanged() -> None:
    """`just check` / `check-all` keep the full suite: `molecule` is the only boundary that still runs it."""
    for recipe_name in ("check", "check-all"):
        recipe = _dry_run(recipe_name)
        assert "just test-cov" in recipe or "pytest --cov" in recipe, f"`just {recipe_name}` no longer reaches the full suite"
        assert "select_impacted_tests.py" not in recipe, (
            f"`just {recipe_name}` now selects by change. `molecule` is the last boundary running the whole "
            "suite before main; making it change-driven leaves nothing that ever runs it."
        )


# --------------------------------------------------------------------------------------------
# Meta: prove the checks above are not vacuously green.
# --------------------------------------------------------------------------------------------


def test_the_classifier_under_test_is_the_shipped_one() -> None:
    """Guard against the guard silently testing a stub or a stale copy."""
    assert SELECTOR.__file__ == str(SELECTOR_PATH), f"loaded {SELECTOR.__file__}, expected {SELECTOR_PATH}"
    assert callable(SELECTOR.classify)
    assert sorted(SELECTOR.GUESS_INFERENCE) == ["call-graph", "filename-pattern", "import-graph"]
    assert sorted(SELECTOR.SOUND_INFERENCE) == ["changed-test"]


def test_the_floor_reader_is_not_silently_empty() -> None:
    """If the comment-stripping ever ate the whole file, every floor assertion above would pass."""
    assert len(_read_floor()) >= 10, "the floor reader returned almost nothing — it is stripping too much"
