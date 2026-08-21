"""Regression coverage for the justfile recipes the beadhive validation boundaries run.

phaze-jnj90 / phaze-nqawu. Two defects that shipped, both of which made a green gate mean
less than it looked like:

* ``just check`` ran ``uv run pytest tests/ -x -q`` -- no coverage (so the coverage figure
  bead criteria ask for could not be produced by the command those criteria name), fail-fast
  (so a red run described only the prefix of the suite), and quiet (so
  ``pytest_report_header`` never printed and CLAUDE.md's "check the database line in the
  pytest header before trusting a green run" was un-followable against the gate's own output).
* the phaze rig had no ``work.validate_cmd``, so ``bh work check`` / ``bh work submit`` fell
  through to the machine-wide default ``sh -c "just lint && just typecheck"`` and ran no
  tests at all.

These assertions are on the recipes, not on a live run, so they are cheap and they fail the
build the moment ``-x``/``-q`` or a lint-only test step comes back.
"""

from pathlib import Path
import re
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE_PATH = REPO_ROOT / "justfile"

# The pytest step every gate must ultimately reach. `just test` (-x -q) is deliberately NOT it.
VALIDATION_TEST_STEP = "just test-cov"


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
        # A caller-exported seat short-circuits the provisioning branch either way; --dry-run
        # never runs it, but pinning the value keeps the output stable between environments.
        env={"TEST_DATABASE_URL": "dry-run", "PATH": str(Path(just).parent) + ":/usr/bin:/bin"},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output


def test_the_validation_test_step_produces_coverage() -> None:
    """Without --cov the 'coverage not below 95%' half of every bead criterion is unsatisfiable."""
    assert "--cov=phaze" in _dry_run("test-cov")


def test_the_validation_test_step_does_not_stop_at_the_first_failure() -> None:
    """A gate characterises the WHOLE suite; -x reduces a red run to a prefix of it."""
    step = _dry_run("test-cov")

    assert not re.search(r"(?<!\S)-x(?!\S)", step), step
    assert not re.search(r"(?<!\S)--exitfirst(?!\S)", step), step


def test_the_validation_test_step_does_not_suppress_the_pytest_header() -> None:
    """-q suppresses pytest_report_header, which is the documented proof of seat isolation."""
    step = _dry_run("test-cov")

    assert not re.search(r"(?<!\S)-q(?!\S)", step), step
    assert not re.search(r"(?<!\S)--quiet(?!\S)", step), step


def test_check_runs_lint_typecheck_and_the_validation_test_step() -> None:
    """`check` is the per-bead beadhive gate; it must reach the coverage-producing step."""
    check = _dry_run("check")

    assert "uv run ruff check" in check
    assert "uv run mypy" in check
    assert VALIDATION_TEST_STEP in check
    # The fail-fast/quiet local-iteration recipe must not be what the gate runs.
    assert not re.search(r"(?<!\S)just test(?!\S)", check), check


def test_check_all_exists_and_is_a_strict_superset_of_check() -> None:
    """`~/.beadhive/config.yaml` names `just check-all` at the molecule / merge-main boundary."""
    check_all = _dry_run("check-all")

    assert "uv run pre-commit run --all-files" in check_all
    assert VALIDATION_TEST_STEP in check_all


def test_the_fail_fast_recipe_is_retained_and_labelled_as_local_iteration() -> None:
    """`just test` keeps -x -q deliberately, and says so, so nobody mistakes it for the gate."""
    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    doc_line, recipe_body = re.search(
        r"^\[doc\('([^']*)'\)\]\n\[group\('test'\)\]\ntest:\n( +.*)$",
        justfile,
        re.MULTILINE,
    ).groups()

    assert "-x" in recipe_body and "-q" in recipe_body
    assert "NOT the validation gate" in doc_line
