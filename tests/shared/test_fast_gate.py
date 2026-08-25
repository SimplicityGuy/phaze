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
