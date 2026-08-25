"""Write a JUnit XML report into bh's drop zone, and ONLY when bh asked for one (phaze-ea6kp).

bh never invokes a test runner. Its whole opt-in surface is one environment variable: before it
runs ``work.validate_cmd`` it creates a fresh empty directory, names it in ``BH_TEST_REPORT_DIR``
in the subprocess environment, and parses whatever JUnit XML turns up there when the command
returns (``beadhive/test_report.py``, bh 0.15.0). A hive that opts into nothing gets an rc-only
ledger entry -- which is what all 49 entries in this repo's ledger were before this module.

WHY THIS IS A HOOK AND NOT AN ``addopts`` LINE. The obvious fix is one line in
``pyproject.toml``::

    addopts = "-m 'not browser' --junitxml=$BH_TEST_REPORT_DIR/pytest.xml"

and it would work inside bh, because pytest *does* expand environment variables in ``--junitxml``
(``_pytest/junitxml.py`` -- ``os.path.expanduser(os.path.expandvars(logfile))``; measured on
pytest 9.1.1). The belief that it does not is widespread and wrong. The defect is everywhere
*else*: ``BH_TEST_REPORT_DIR`` is set only inside bh validation subprocesses, and
``os.path.expandvars`` leaves an **undefined** variable untouched rather than substituting empty
(``expandvars('$UNSET/x') -> '$UNSET/x'``). So that line litters a directory literally named
``$BH_TEST_REPORT_DIR`` into the working directory of every plain ``uv run pytest``, every
``just test``, and every CI run. Gating on the variable's presence is the only shape that leaves
those byte-for-byte unchanged.

THREE DELIBERATE CHOICES.

*The report is named* ``pytest.xml``. bh globs ``*.xml`` over the drop zone and merges every file
it finds, so the name is free and its only job is to say which runner wrote it. Should a second
tier ever drop a report in the same directory, two runner-named files merge correctly where two
files both called ``report.xml`` would collide.

*An explicit* ``--junitxml`` *always wins.* Two recipes in this repo already pass one --
``just test-bucket`` (``--junitxml=junit.xml``, the CI shards) and ``just repowise-coverage`` --
and silently redirecting their report because a validation happened to be the caller would break
a working path to serve a diagnostic one.

*Change-selected runs report too.* ``just check-fast`` has two shapes -- a subset RUN and an
ESCALATE that delegates to the full suite -- and both share one ``cmd_hash``, so the ledger cannot
tell them apart on its own. The count is what tells them apart: a few hundred against ~8,000 is
unmistakable, and it is the ONLY machine-readable discriminator available. Suppressing the subset
report would delete that signal and replace it with an absent field, which is ambiguous between
three different causes (subset run / opt-in broken / bh too old) -- the exact ambiguity this
module exists to remove. Cross-recipe confusion is impossible anyway: ``cmd_hash`` is part of the
ledger key, so a ``just check-fast`` verdict is only ever replayed by another ``just check-fast``.

WHAT THIS MUST NEVER DO. bh's first binding constraint is that ``rc`` is the verdict and the
report is detail that may never upgrade one. Nothing here reads a test outcome, an exit status or
the report itself; the only thing it decides is a file path, before a single test has run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


#: bh's one variable (``beadhive.test_report.ENV_VAR``). Set only inside a validation subprocess.
ENV_VAR = "BH_TEST_REPORT_DIR"

#: The report's basename inside the drop zone. Names the runner, because bh merges every
#: ``*.xml`` in that directory.
REPORT_NAME = "pytest.xml"

#: Name this plugin is registered under, so ``-p no:phaze-bh-test-report`` can disable it.
PLUGIN_NAME = "phaze-bh-test-report"


def drop_zone() -> Path | None:
    """The directory bh asked for a report in, or ``None`` when nothing asked.

    An unset *or empty* variable means no drop zone: bh always exports a real path, so an empty
    value is a broken environment rather than a request, and the safe reading of a broken request
    is the one that writes nothing.
    """
    value = os.environ.get(ENV_VAR, "").strip()
    return Path(value) if value else None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Point pytest's JUnit writer at bh's drop zone, when there is one.

    Must run before ``_pytest.junitxml``'s own ``pytest_configure``, which is where
    ``config.option.xmlpath`` is read and the writer built. Two independent things secure that:
    the registration point (``tests/conftest.py`` registers this module from inside its own
    ``tryfirst`` ``pytest_configure``, so pluggy replays the in-flight historic call here
    immediately), and ``tryfirst`` on this implementation, which is what orders it correctly when
    the module is loaded directly with ``-p tests.bh_test_report`` instead.

    The ``hasattr`` arm is not defensive noise: under ``-p no:junitxml`` the option does not
    exist at all, and there is then no writer to aim anywhere. Returning without creating the
    attribute keeps this hook a no-op on such a run rather than leaving behind a setting that
    reads as configuration and is wired to nothing.
    """
    drop = drop_zone()
    if drop is None:
        return
    if not hasattr(config.option, "xmlpath") or config.option.xmlpath:
        return
    config.option.xmlpath = str(drop / REPORT_NAME)


def pytest_report_header() -> str | None:
    """Say in the header that a report is going to bh, and where.

    Deliberately phrased as configuration rather than outcome: the header prints before the run
    and the file is written at session finish, so this states what was *asked for*. The ledger
    entry is what says a report was produced and read.
    """
    drop = drop_zone()
    if drop is None:
        return None
    return f"bh test report: writing {REPORT_NAME!r} to {str(drop)!r} (from {ENV_VAR})"
