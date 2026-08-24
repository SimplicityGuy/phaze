"""Coverage settings live in ONE place each, and the places actually work (phaze-jktlb).

Filed after phaze-bk9el.21 at operator request: "move common coverage settings into
[the coverage run table]. for instance .json/.xml should always be emitted. also, we need to
ensure that that setting is, in fact, honored", and amended at dispatch with "we need to make
sure which settings can be moved to the pyproject.toml and which can't; are the settings even
read from the pyproject.toml? and if not, where can they live? and last, make sure the just
recipes put the parameters in the same order, preferably in alphabetical order".

THE ANSWER THE RESEARCH REACHED, because these tests only make sense against it. Measured on
coverage.py 7.15.4 + pytest-cov 7.1.0, from artifacts rather than from documentation:

* coverage.py's own settings ARE read from ``pyproject.toml``. pytest-cov passes ``--cov-config``
  default ``.coveragerc``; coverage.py normalises that exact string back to "unspecified", which
  re-enables its fallback chain, and this repo has no ``.coveragerc``/``setup.cfg``/``tox.ini``, so
  ``pyproject.toml`` is what gets read. That covers branch, concurrency, omit, relative_files,
  source, fail_under, precision, show_missing -- and now the json/xml output PATHS.
* report SELECTION cannot go there, and cannot go into pytest's ini table either: ``--cov-report``
  is pytest-cov's, and pytest-cov registers eleven command-line options and ZERO ini keys. Its home
  is the justfile's ``cov_reports`` variable, which AC3 names as the sanctioned second-best.
* ``--cov-context=test`` and ``--cov-fail-under=0`` stay per-recipe on the shards, deliberately.
  ``dynamic_context = "test_function"`` is NOT an equivalent: it records bare function names where
  ``--cov-context=test`` records pytest nodeids, pytest-cov warns when it sees it, and with both
  set coverage.py reports a dynamic-context conflict and THE CONFIG WINS -- silently degrading the
  contexts repowise's per-test map is built from.

WHAT THESE TESTS ARE FOR. AC6: fail if a recipe stops emitting an artifact, if a setting leaves
its central location, or if a recipe overrides the central setting with a CLI flag. Where a claim
can be checked by BEHAVIOUR it is -- the destination assertions below call coverage.py itself with
the repo's real config rather than reading the key back, because a key that is present but not
honored is worse than an absent one (phaze-jnj90, phaze-nqawu, and phaze-bk9el.21's own first
draft are the three times this repo has paid for the difference).
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import coverage
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE_PATH = REPO_ROOT / "justfile"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
REPOWISE_SCRIPT_PATH = REPO_ROOT / "scripts" / "repowise-coverage.sh"

# The recipes that measure the WHOLE suite and must therefore leave a report every consumer can
# read. `just branch-check` and scripts/coverage_floor.py read coverage.json; Codecov reads
# coverage.xml. Neither should depend on which of these the developer happened to run (AC4).
WHOLE_SUITE_RECIPES = ("test-ci", "test-cov")

# The CI shard is deliberately NOT in the list above; see `test_the_ci_shard_stays_report_free`.
SHARD_RECIPE = "test-bucket"

# Every pytest-cov flag that would re-specify something pyproject.toml already owns. `--cov=<x>`
# duplicates `source`; a `--cov-report=<type>:<DEST>` duplicates the json/xml `output` keys;
# `--cov-precision` duplicates `precision`; `--cov-branch`/`--no-branch` duplicate `branch`; an
# rcfile redirect bypasses the config file wholesale.
_CENTRAL_SETTING_OVERRIDES = (
    (re.compile(r"--cov=\S"), "--cov=<source>", 'source = ["phaze"] in the coverage run table'),
    (re.compile(r"--cov-report=(?:json|xml|html|lcov|annotate|markdown)\S*:"), "--cov-report=<type>:<DEST>", "the json/xml output keys"),
    (re.compile(r"--cov-precision"), "--cov-precision", "precision in the coverage report table"),
    (re.compile(r"--cov-branch"), "--cov-branch", "branch in the coverage run table"),
    (re.compile(r"--no-branch"), "--no-branch", "branch in the coverage run table"),
    (re.compile(r"--rcfile|COVERAGE_RCFILE"), "an rcfile redirect", "pyproject.toml itself"),
    (re.compile(r"coverage (?:json|xml)[^\n]*\s-o\s"), "coverage json/xml -o <DEST>", "the json/xml output keys"),
)


@functools.cache
def _dry_run(*recipe_and_args: str) -> str:
    """Return what ``just`` would execute, without executing it.

    Cached: these tests ask the same handful of recipes repeatedly (once per parametrize id at
    collection, then again inside each test), and a `just` subprocess per question turns a
    sub-second module into a slow one for no extra assurance -- the justfile cannot change
    mid-session.
    """
    just = shutil.which("just")
    if just is None:  # pragma: no cover - `just` is a documented prerequisite of this repo
        pytest.skip("just is not on PATH")
    result = subprocess.run(  # noqa: S603 - fixed executable, recipe names are literals above
        [just, "--dry-run", *recipe_and_args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        # A caller-exported seat short-circuits `test-validate`'s provisioning branch either way;
        # --dry-run never runs it, but pinning the value keeps the output stable between machines.
        env={"TEST_DATABASE_URL": "dry-run", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def _pytest_command(*recipe_and_args: str) -> str:
    """Return the single ``pytest`` command line a recipe would run."""
    lines = [line for line in _dry_run(*recipe_and_args).splitlines() if " pytest " in f" {line} "]
    assert len(lines) == 1, f"expected exactly one pytest command in {recipe_and_args}, got: {lines}"
    return lines[0]


def _cov_flags(command: str) -> list[str]:
    """Return the ``--cov*`` flags of a command line, in the order they appear."""
    return re.findall(r"--cov[\w-]*(?:=\S*)?", command)


def _repo_coverage(**overrides: object) -> coverage.Coverage:
    """A Coverage object configured EXACTLY as every recipe in this repo configures one.

    The real consumer, handed the real config file -- so a key that pyproject sets but
    coverage.py ignores fails here rather than reading as configured (ADR-0012 rule 3).
    """
    return coverage.Coverage(config_file=str(PYPROJECT_PATH), **overrides)  # type: ignore[arg-type]


# --- the settings that CAN be central, are, and are honored --------------------------------------


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("run:branch", True),
        ("run:source", ["phaze"]),
        ("run:relative_files", True),
        ("report:show_missing", True),
        ("report:fail_under", 95),
        ("json:output", "coverage.json"),
        ("xml:output", "coverage.xml"),
    ],
)
def test_coverage_py_reads_each_central_setting_from_pyproject(option: str, expected: object) -> None:
    """AC2/AC3. Asked of coverage.py, not of tomllib.

    This is the "are the settings even read from the pyproject.toml?" question, put to the
    component that would have to read them. A `[tool.coverage.*]` key that coverage.py does not
    recognise, or one shadowed by a stray `.coveragerc` appearing in the repo root, fails here.

    For five of the seven, coverage.py's built-in default DIFFERS from the configured value, so
    deleting the key fails this test. For ``json:output`` and ``xml:output`` it does not -- see
    the next test, which is where their removal is caught.
    """
    assert _repo_coverage().get_option(option) == expected


def test_the_report_destinations_are_stated_in_pyproject_and_not_merely_defaulted_into() -> None:
    """AC6's "a setting is removed from the central location", for the two keys behaviour can't see.

    ``coverage.json`` and ``coverage.xml`` are ALSO coverage.py's built-in defaults, so
    ``get_option`` returns the right answer whether the keys are configured or absent. The
    behaviour check above is therefore blind to their deletion, and a guard that cannot fail is
    the exact defect this bead was filed about -- so this one reads the file.

    Being stated is the point rather than a formality: these keys are what let ``test-cov`` drop
    ``--cov-report=json:coverage.json`` and ``repowise-coverage.sh`` drop ``-o coverage.xml``.
    Delete them and nothing breaks today, but the path silently reverts to being decided by
    coverage.py's defaults, and the next recipe that wants a different one re-hardcodes it.
    """
    tables = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["tool"]["coverage"]

    assert tables.get("json", {}).get("output") == "coverage.json", "the json output path is no longer stated in pyproject.toml"
    assert tables.get("xml", {}).get("output") == "coverage.xml", "the xml output path is no longer stated in pyproject.toml"


def test_the_json_and_xml_destinations_are_honored_not_merely_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5, permanently. The recipes name no `:DEST`, so the config must supply one.

    pytest-cov's engine calls ``cov.json_report(outfile=None)`` / ``cov.xml_report(outfile=None)``
    whenever ``--cov-report=json`` / ``=xml`` carry no ``:DEST``, and coverage.py then resolves the
    path from config. That is the whole mechanism the recipes now depend on, so it is exercised
    here the same way: report with no outfile, and assert the files appear where pyproject says.

    A regression to coverage.py's built-in defaults would NOT be caught by comparing the key --
    the defaults happen to be the same two names. It is caught here, because a key coverage.py
    stopped honoring produces a file at the default path while the key still reads correctly.
    """
    module = tmp_path / "branchy.py"
    module.write_text("def f(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cov = _repo_coverage(data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)])
    cov.start()
    exec(compile(module.read_text(encoding="utf-8"), str(module), "exec"), {})  # noqa: S102 - throwaway module, fixture input
    cov.stop()

    cov.json_report()
    cov.xml_report()

    expected_json = str(_repo_coverage().get_option("json:output"))
    expected_xml = str(_repo_coverage().get_option("xml:output"))
    assert (tmp_path / expected_json).is_file(), f"json_report() with no outfile did not honour the configured path {expected_json!r}"
    assert (tmp_path / expected_xml).is_file(), f"xml_report() with no outfile did not honour the configured path {expected_xml!r}"


# --- AC4: every whole-suite recipe emits BOTH artifacts ------------------------------------------


@pytest.mark.parametrize("recipe", WHOLE_SUITE_RECIPES)
@pytest.mark.parametrize("report", ["json", "xml"])
def test_every_whole_suite_recipe_requests_both_file_reports(recipe: str, report: str) -> None:
    """AC4. No consumer may be coupled to whichever recipe the developer chose."""
    flags = _cov_flags(_pytest_command(recipe))

    assert f"--cov-report={report}" in flags, f"`just {recipe}` no longer emits {report}: {flags}"


@pytest.mark.parametrize("recipe", WHOLE_SUITE_RECIPES)
def test_every_whole_suite_recipe_takes_its_reports_from_the_shared_variable(recipe: str) -> None:
    """AC3: ONE place decides report selection, so the recipes must agree exactly.

    Asserting equality against the variable -- rather than "json and xml appear somewhere" --
    is what makes a recipe drifting its own extra report, or dropping the terminal one, fail.
    """
    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    declared = re.search(r'^cov_reports := "([^"]*)"$', justfile, re.MULTILINE)
    assert declared is not None, "the cov_reports variable is gone; report selection has no single home"

    assert _cov_flags(_pytest_command(recipe))[1:] == declared.group(1).split()


def test_coverage_combine_emits_both_artifacts_before_it_gates() -> None:
    """AC4 for the CI fan-in, plus the ordering that keeps a red run's evidence.

    `coverage report` sorts between `json` and `xml`, so straight alphabetical order would put the
    gate in the middle of the artifacts. Artifacts first, gates second, each group alphabetical --
    a stated deviation, not an oversight.

    MEASURED during phaze-jktlb over deliberately partial shards, because the ordering was
    otherwise just an argument. The pre-jktlb form (`xml` / `json` / `report`, no `--fail-under=0`
    on the artifact steps) exited at `coverage xml`: a report writer writes its file and only then
    exits on the inherited floor, so coverage.xml survived and `coverage json` never ran at all.
    coverage.json is what `scripts/coverage_floor.py` and `just branch-check` read, so a sub-floor
    combine destroyed the artifact naming the module that dropped. The current form wrote both in
    full and then failed at `coverage report --fail-under=95`.
    """
    steps = [line.strip() for line in _dry_run("coverage-combine").splitlines() if line.strip()]
    subcommands = [re.sub(r"^uv run coverage ", "", step).split()[0] for step in steps if step.startswith("uv run coverage ")]

    assert subcommands == ["combine", "json", "xml", "report"], subcommands
    assert steps[-1].endswith("scripts/coverage_floor.py"), steps


# --- AC6: no recipe may override a setting that has a central home -------------------------------


def _all_coverage_sites() -> dict[str, str]:
    """Every command line in the repo that measures or reports coverage."""
    sites = {recipe: _pytest_command(recipe) for recipe in WHOLE_SUITE_RECIPES}
    sites[SHARD_RECIPE] = _pytest_command(SHARD_RECIPE, "shard", "tests/shared")
    sites["coverage-combine"] = _dry_run("coverage-combine")
    # The sixth site, and the one the bead's own inventory missed: `just repowise-coverage`.
    script = REPOWISE_SCRIPT_PATH.read_text(encoding="utf-8")
    sites["repowise-coverage.sh"] = "\n".join(
        line
        for line in script.splitlines()
        if not line.lstrip().startswith("#") and ("pytest " in line or "coverage xml" in line or "coverage json" in line)
    )
    return sites


@pytest.mark.parametrize("site", sorted(_all_coverage_sites()))
def test_no_coverage_site_overrides_a_setting_that_lives_in_pyproject(site: str) -> None:
    """AC6's third clause. A CLI flag silently wins over config, so it must not be there.

    This is the failure mode that makes centralization worthless rather than merely incomplete:
    the config keeps reading as the source of truth while a recipe quietly disagrees with it, and
    the two only diverge for whoever runs that one recipe. `coverage report --fail-under=95` in
    `coverage-combine` is the ONE deliberate exception -- it is required to stay in lockstep with
    pyproject by tests/shared/test_coverage_gate.py, which is a stronger guarantee than absence.
    """
    command = _all_coverage_sites()[site]

    for pattern, flag, home in _CENTRAL_SETTING_OVERRIDES:
        assert not pattern.search(command), f"{site} passes {flag}, which overrides {home}:\n{command}"


# --- Operator instruction 2026-08-22: consistent, alphabetical parameter order --------------------


@pytest.mark.parametrize("site", ["coverage-combine", *WHOLE_SUITE_RECIPES, SHARD_RECIPE])
def test_every_coverage_site_orders_its_cov_flags_alphabetically(site: str) -> None:
    """Operator instruction, phaze-jktlb, 2026-08-22: "preferably in alphabetical order".

    A legibility requirement, and the reason it earns a test: before this bead the four recipes
    disagreed about BOTH which coverage flags they passed and the order they passed them in, and
    the second made the first hard to see by diffing them side by side. Alphabetical is the
    default; a deviation needs a stated reason (coverage-combine's artifacts-before-gates split
    has one, and is asserted separately above).
    """
    if site == "coverage-combine":
        pytest.skip("coverage-combine passes subcommands, not --cov flags; its order is asserted above")
    command = _pytest_command(site) if site in WHOLE_SUITE_RECIPES else _pytest_command(SHARD_RECIPE, "shard", "tests/shared")
    flags = _cov_flags(command)

    assert flags == sorted(flags), f"{site} passes its coverage flags out of alphabetical order: {flags}"


# --- AC7: the two load-bearing per-recipe settings survived centralization -----------------------


def test_the_ci_shard_stays_report_free_and_keeps_its_two_deliberate_overrides() -> None:
    """AC7, and the one place where "every recipe emits both artifacts" is deliberately not applied.

    Three claims, all measured during phaze-jktlb rather than reasoned about:

    * ``--cov-report=`` IS NOT A RESET. pytest-cov's StoreReport does
      ``cov_report[report_type] = file``, so an empty value adds the key ``""`` beside whatever has
      already accumulated. Putting json/xml in pytest's ``addopts`` left BOTH artifacts written by
      a run that also passed ``--cov-report=``. That is why report selection lives in a just
      variable this recipe does not reference, rather than in ``addopts``.
    * ``COVERAGE_FILE`` redirects only the binary DATA file. A shard emitting reports would write
      them to the SHARED coverage.json/coverage.xml names, overwriting a full run's -- and
      ``just branch-check`` reads exactly that coverage.json.
    * ``--cov-fail-under=0`` and ``--cov-context=test`` are per-recipe by design (AC7). The
      context flag has no working central equivalent: ``dynamic_context = "test_function"`` records
      function names instead of nodeids and, set alongside this flag, wins while emitting only a
      warning.
    """
    flags = _cov_flags(_pytest_command(SHARD_RECIPE, "shard", "tests/shared"))

    assert "--cov-report=" in flags, "the shard must suppress reports; a partial one would clobber a full run's"
    assert not any(flag.startswith("--cov-report=") and flag != "--cov-report=" for flag in flags), flags
    assert "--cov-fail-under=0" in flags, "a shard's PARTIAL coverage must not be measured against the global floor"
    assert "--cov-context=test" in flags, "the binary shard is the only CI artifact that can carry per-test contexts"


def test_running_the_shard_flag_set_actually_writes_no_report(tmp_path: Path) -> None:
    """The shard exclusion, asserted by EXECUTION rather than by reading the flags back.

    The exclusion rests on an operator decision taken on 2026-08-22 and recorded on bead
    phaze-jktlb -- which is the durable record; that same decision required pinning it here so
    `test-bucket` cannot silently drift back into emitting.

    The test above reads the
    recipe's flags; this one RUNS them, because the whole reason the exclusion is fragile is that
    ``--cov-report=`` looks like a reset and is not one -- a future edit that leaves the flag in
    place while adding reports from anywhere else would satisfy a text check and still clobber
    coverage.json.

    The flag list comes from the real recipe via ``just --dry-run``. Only the measured SOURCE
    differs (a throwaway module here rather than phaze), because the flags under test are the
    report and gate flags, and coverage of zero files would prove nothing about either. The
    destination names come from the repo's own config, so a report that did get written would land
    exactly where a real shard would have written it -- on top of a full run's artifacts.
    """
    flags = _cov_flags(_pytest_command(SHARD_RECIPE, "shard", "tests/shared"))
    json_name = str(_repo_coverage().get_option("json:output"))
    xml_name = str(_repo_coverage().get_option("xml:output"))

    (tmp_path / "shardy.py").write_text("def f(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8")
    (tmp_path / "test_shardy.py").write_text("from shardy import f\n\n\ndef test_f():\n    assert f(1) == 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["."]\n'
        "\n[tool.coverage.json]\n"
        f'output = "{json_name}"\n'
        "\n[tool.coverage.report]\n"
        # Armed, so `--cov-fail-under=0` has something to override: one arm of `f` is never taken.
        "fail_under = 95\n"
        "\n[tool.coverage.run]\n"
        "branch = true\n"
        'source = ["shardy"]\n'
        "\n[tool.coverage.xml]\n"
        f'output = "{xml_name}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603 - sys.executable plus flags taken from this repo's own recipe
        [sys.executable, "-m", "pytest", *flags, "-p", "no:cacheprovider", "-q"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "COVERAGE_FILE": ".coverage.shardprobe", "PYTHONPATH": str(tmp_path)},
    )

    assert result.returncode == 0, f"--cov-fail-under=0 did not disarm the floor:\n{result.stdout}{result.stderr}"
    assert not (tmp_path / json_name).exists(), f"a shard wrote {json_name}, which would overwrite a full run's"
    assert not (tmp_path / xml_name).exists(), f"a shard wrote {xml_name}, which would overwrite a full run's"
    assert (tmp_path / ".coverage.shardprobe").is_file(), "the shard did not write its binary data file"

    data = coverage.CoverageData(str(tmp_path / ".coverage.shardprobe"))
    data.read()
    contexts = [c for c in data.measured_contexts() if c]
    assert any("::" in c for c in contexts), f"--cov-context=test did not record pytest nodeids: {contexts}"


def test_the_central_config_does_not_try_to_own_the_per_test_context() -> None:
    """AC7's second half, asserted where it would actually go wrong.

    Setting ``dynamic_context`` in pyproject looks like the natural completion of this bead and is
    a trap: measured, it degrades the shards' contexts from pytest nodeids
    (``tests/test_f.py::test_f|run``) to bare function names (``test_f.test_f``), pytest-cov emits
    CentralCovContextWarning, and with ``--cov-context=test`` also set coverage.py reports a
    dynamic-context conflict and the CONFIG value wins. repowise's per-test map is built from those
    contexts, so the damage would be silent and would only surface as an empty ``tests_to_run``.
    """
    assert _repo_coverage().get_option("run:dynamic_context") is None, (
        "pyproject sets run:dynamic_context, which overrides pytest-cov's --cov-context=test and "
        "degrades the shards' per-test contexts to bare function names"
    )
