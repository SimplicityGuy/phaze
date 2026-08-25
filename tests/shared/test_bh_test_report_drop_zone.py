"""Guards for the ``BH_TEST_REPORT_DIR`` opt-in (phaze-ea6kp).

The feature is one conditional: write a JUnit XML report when — and only when — bh's validation
subprocess asked for one. Both halves of that condition are load-bearing and both are asserted
here, because each fails in a way the other cannot reveal:

* **Variable set, nothing written** is the state the repo was already in. It is invisible: every
  gate stays green and the ledger simply keeps recording rc-only verdicts, which is bh's
  documented normal for a hive that opted into nothing.
* **Variable unset, something written** is the defect the obvious ``addopts`` fix produces. pytest
  expands environment variables in ``--junitxml`` and ``os.path.expandvars`` leaves an *undefined*
  variable untouched, so an unconditional config line drops a directory literally named
  ``$BH_TEST_REPORT_DIR`` into the working directory of every plain ``uv run pytest``, every
  ``just test`` and every CI run. That is asserted by name below, not merely as "no XML".

**What the subprocess tests prove, and what they do not.** They run a real pytest and inspect a
real filesystem, so they discharge "pytest writes the file where the hook aimed it". The *real*
consumer is bh's ``test_report.ingest`` / ``counts``
(``docs/design/0012-verification-fidelity-and-operator-attribution.md`` rule 3), and
``test_bh_ingests_the_report_this_hook_produces`` hands the file to bh's own parser under bh's own
interpreter when bh is installed. Even that is a component check: the criterion this bead is
discharged by is a ``.git/bh-validation-ledger.json`` entry carrying a ``report`` field after a
real ``bh work check``, which no test can produce.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from tests import bh_test_report


REPO_ROOT = Path(__file__).resolve().parents[2]

# A whole test suite in one file: enough to give the report a non-zero count without dragging the
# repo's conftest, database guard or session lock into the nested run.
_NESTED_SUITE = """
def test_one():
    assert True


def test_two():
    assert True
"""


def _fake_config(xmlpath: str | None = None) -> SimpleNamespace:
    """The two attributes ``pytest_configure`` touches, and nothing else."""
    return SimpleNamespace(option=SimpleNamespace(xmlpath=xmlpath))


# ------------------------------------------------------------------------------------------
# The condition itself.
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_variable_is_not_a_drop_zone(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """An empty value is a broken environment, not a request — and writing to ``Path("")`` would
    resolve to the working directory, which is precisely the litter this feature must not create."""
    monkeypatch.setenv(bh_test_report.ENV_VAR, value)
    assert bh_test_report.drop_zone() is None


def test_an_unset_variable_is_not_a_drop_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bh_test_report.ENV_VAR, raising=False)
    assert bh_test_report.drop_zone() is None
    config = _fake_config()
    bh_test_report.pytest_configure(config)
    assert config.option.xmlpath is None
    assert bh_test_report.pytest_report_header() is None


def test_a_set_variable_aims_pytest_at_the_drop_zone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bh_test_report.ENV_VAR, str(tmp_path))
    config = _fake_config()
    bh_test_report.pytest_configure(config)
    assert config.option.xmlpath == str(tmp_path / bh_test_report.REPORT_NAME)
    header = bh_test_report.pytest_report_header()
    assert header is not None
    assert str(tmp_path) in header


def test_an_explicit_junitxml_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``just test-bucket`` (the CI shards) and ``just repowise-coverage`` both pass their own
    ``--junitxml``. Redirecting a working path to serve a diagnostic one would be a regression,
    so the caller's choice is never overwritten."""
    monkeypatch.setenv(bh_test_report.ENV_VAR, str(tmp_path))
    config = _fake_config(xmlpath="junit.xml")
    bh_test_report.pytest_configure(config)
    assert config.option.xmlpath == "junit.xml"


def test_a_disabled_junitxml_plugin_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Under ``-p no:junitxml`` the option does not exist at all. There is no writer to aim, so
    returning quietly is the only behaviour that does not break an otherwise valid run."""
    monkeypatch.setenv(bh_test_report.ENV_VAR, str(tmp_path))
    config = SimpleNamespace(option=SimpleNamespace())
    bh_test_report.pytest_configure(config)
    assert not hasattr(config.option, "xmlpath")


# ------------------------------------------------------------------------------------------
# rc stays authoritative (bh's binding constraint 1).
# ------------------------------------------------------------------------------------------


def test_the_module_implements_no_hook_that_can_move_an_exit_code() -> None:
    """The report is detail, never a verdict, and may never upgrade one.

    Asserted structurally rather than by reading the source for forbidden words: the only two
    hooks this module implements are ``pytest_configure`` (which runs before collection, so no
    outcome exists yet) and ``pytest_report_header`` (whose return value is printed). Neither can
    influence an exit status. A future ``pytest_sessionfinish`` or ``pytest_collection_modifyitems``
    here would fail this test and demand the argument be made again.
    """
    implemented = {name for name in vars(bh_test_report) if name.startswith("pytest_")}
    assert implemented == {"pytest_configure", "pytest_report_header"}


def test_conftest_registers_the_plugin() -> None:
    """The wiring, which the subprocess tests below deliberately bypass with ``-p``.

    ``tests/conftest.py`` is what makes this live for every ordinary run; losing that line would
    leave every unit test above passing over a feature that never fires.
    """
    source = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "bh_test_report" in source
    assert "config.pluginmanager.register(bh_test_report, bh_test_report.PLUGIN_NAME)" in source
    assert "is_registered(bh_test_report)" in source, "double registration raises -- the guard is load-bearing"


# ------------------------------------------------------------------------------------------
# A real pytest, a real filesystem.
# ------------------------------------------------------------------------------------------


def _run_nested_pytest(workdir: Path, drop: Path | None) -> subprocess.CompletedProcess[str]:
    """Run a two-test suite in an isolated rootdir, with the drop zone set or unset.

    ``-p tests.bh_test_report`` loads the plugin directly, which is why ``PYTHONPATH`` names the
    repo root. That deliberately bypasses ``tests/conftest.py`` — a nested run that loaded it
    would take this worktree's database seat and its session advisory lock away from the suite
    currently running. ``test_conftest_registers_the_plugin`` covers the wiring instead.
    """
    (workdir / "test_nested.py").write_text(_NESTED_SUITE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != bh_test_report.ENV_VAR}
    env["PYTHONPATH"] = str(REPO_ROOT)
    if drop is not None:
        env[bh_test_report.ENV_VAR] = str(drop)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.bh_test_report", "-p", "no:cacheprovider", "-q", "test_nested.py"],
        cwd=workdir,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_real_run_writes_the_report_into_the_drop_zone(tmp_path: Path) -> None:
    workdir, drop = tmp_path / "work", tmp_path / "drop"
    workdir.mkdir()
    drop.mkdir()

    result = _run_nested_pytest(workdir, drop)
    assert result.returncode == 0, result.stdout + result.stderr

    report = drop / bh_test_report.REPORT_NAME
    assert report.is_file(), sorted(p.name for p in drop.iterdir())
    # Stdlib parser, for the same reason bh's own ingest uses one: this file was written
    # moments ago by a pytest THIS test started. Same trust domain, strictly less powerful.
    root = ET.parse(report).getroot()  # noqa: S314 - self-produced artifact, not untrusted input
    assert len(list(root.iter("testcase"))) == 2
    # Nothing leaked into the working directory: the report went to the drop zone, not beside it.
    assert list(workdir.glob("*.xml")) == []


def test_an_unset_variable_writes_nothing_anywhere(tmp_path: Path) -> None:
    """The half that guards every non-bh run, and it names the failure mode explicitly."""
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = _run_nested_pytest(workdir, drop=None)
    assert result.returncode == 0, result.stdout + result.stderr

    assert not (workdir / f"${bh_test_report.ENV_VAR}").exists()
    assert list(workdir.rglob("*.xml")) == []
    assert list(tmp_path.rglob("*.xml")) == []


# ------------------------------------------------------------------------------------------
# The real consumer, where it is available.
# ------------------------------------------------------------------------------------------


def _bh_interpreter() -> Path | None:
    """bh's own interpreter, or ``None``.

    bh ships as a self-contained install with its own Python; ``beadhive.test_report`` imports
    ``typer``, which this repo's venv does not have, so the parser cannot simply be imported here.
    Returning ``None`` — and skipping — is correct rather than lax: CI has no bh at all, and a
    layout change upstream should not turn this repo's suite red over a diagnostic check.
    """
    bh = shutil.which("bh")
    if bh is None:
        return None
    # The launcher's own shebang, which is the only authority on which interpreter bh runs under
    # — deriving the path from the install layout instead guesses, and guessed wrong first try
    # (`bh` resolves straight into `libexec/bin/`, not into a sibling of it).
    shebang = Path(bh).resolve().read_text(encoding="utf-8", errors="replace").splitlines()[0]
    if not shebang.startswith("#!"):
        return None
    python = Path(shebang.removeprefix("#!").strip())
    return python if python.is_file() else None


def test_bh_ingests_the_report_this_hook_produces(tmp_path: Path) -> None:
    """Rule 3: hand the artifact to its real consumer, not to the tool that produced it.

    Asserting the XML is well-formed proves pytest can round-trip its own format. What matters is
    that ``beadhive.test_report.ingest`` returns counts for it — that is the function whose
    ``None`` return is why all 49 ledger entries were rc-only.
    """
    python = _bh_interpreter()
    if python is None:
        pytest.skip("bh is not installed, or its bundled interpreter is not where it used to be")

    workdir, drop = tmp_path / "work", tmp_path / "drop"
    workdir.mkdir()
    drop.mkdir()
    assert _run_nested_pytest(workdir, drop).returncode == 0

    probe = (
        "import json;from pathlib import Path;from beadhive import test_report;"
        "print(json.dumps(test_report.counts(test_report.ingest(Path(__import__('sys').argv[1]), 0))))"
    )
    result = subprocess.run(  # noqa: S603 - a path resolved from `which bh`, and a literal probe
        [str(python), "-c", probe, str(drop)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"bh's bundled interpreter could not import beadhive.test_report: {result.stderr.strip()}")

    counts = json.loads(result.stdout.strip().splitlines()[-1])
    assert counts == {"tests": 2, "passed": 2, "failures": 0, "errors": 0, "skipped": 0}
