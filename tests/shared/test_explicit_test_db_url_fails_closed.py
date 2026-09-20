"""An EXPLICIT ``TEST_DATABASE_URL`` must fail CLOSED on a broken connect (phaze-34gkn).

Spike phaze-cpn5k measured the gap this closes: ``acquire_exclusive_session_lock`` returns
``None`` -- no lock, no complaint -- whenever ``psycopg.connect`` raises ``OperationalError``,
which includes simply outrunning ``connect_timeout=5``. Against a blackholed address that
produced ``ELAPSED=7s EXIT=0``, a full pass, and the only trace was the word ``unlocked`` in a
pytest header that ``-q`` suppresses. A SLOW harness (a loaded box, six seats gating at once) is
indistinguishable from an ABSENT one under a mere timeout, so this is the one shape in which two
pytest processes can share one database today without either being refused.

Operator decision, phaze-34gkn, 2026-09-16, recorded by disp/fable. Question as put
(AskUserQuestion, that dispatch session): "phaze-34gkn: the pytest advisory lock returns no lock
when its 5-second Postgres connect fails, including a timeout, so a slow harness reads as absent
and two suites can share a database with only the word unlocked in a header that -q hides. Which
behaviour should it have?" Answer as given (selected option label, verbatim): "Fail closed when
URL is explicit (Recommended)". Durable record: bead phaze-34gkn comment, 2026-09-16 21:45, and
docs/gates-and-isolation.md next to the phaze-cpn5k section.

What shipped: ``acquire_exclusive_session_lock(dsn, explicit=...)`` raises
``TestDatabaseUnreachableError`` instead of returning ``None`` when ``explicit=True`` and the
connect fails. ``tests/conftest.py``'s ``pytest_sessionstart`` passes
``explicit=bool(os.environ.get("TEST_DATABASE_URL"))`` -- true precisely when the RUN's own
environment carried the variable, never when this module's own default filled it in -- and turns
the raised error into the same ``pytest.exit(..., USAGE_ERROR)`` shape as
``SharedTestDatabaseError``. These tests spawn a REAL second ``pytest`` process (the same
end-to-end shape as ``test_test_db_session_exclusivity.py``'s real-subprocess test) so the
assertion is against the actual wiring, not just the helper in isolation.

The subprocess target is ``tests/shared/_fixture_trivial_db_free.py``, never THIS file: this
module's own tests spawn subprocesses, so pointing a subprocess at this module would re-collect
these very tests inside it and recurse. The fixture module is trivial and DB-free on purpose.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_TARGET = str(Path(__file__).resolve().parent / "_fixture_trivial_db_free.py")

# A private-use-range address that this host does not route anywhere listening (the same address
# the phaze-cpn5k spike measured against, 2026-09-16). Connecting to it does not refuse instantly
# like a closed local port would -- the SYN goes out and nothing answers, so the connection blocks
# until `connect_timeout` elapses. That "slow, not instantly absent" shape is exactly what the
# phaze-34gkn, 2026-09-16 operator decision is about; a fast ECONNREFUSED would not exercise it.
_BLACKHOLE_DSN = "postgresql+asyncpg://phaze:phaze@10.255.255.1:5433/phaze_unreachable_test"

# A port nothing listens on, on loopback. Unlike the blackhole address this refuses FAST
# (ECONNREFUSED), which is what the "default URL still fails open" test wants: it only needs to
# prove the exemption survives, not re-spend a `connect_timeout` proving fail-open a second way.
_UNUSED_LOCAL_PORT = "1"


def _run_subprocess_suite(*, env_overrides: dict[str, str], env_removals: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    """Spawn a real ``pytest`` subprocess against the trivial fixture module, with an overridden env.

    Targeting the fixture module keeps the subprocess's collected set to two always-passing,
    DB-free tests while still going through the REAL ``tests/conftest.py`` machinery --
    ``pytest_sessionstart`` fires for any target, including this one, unconditional on which tests
    were selected.
    """
    env = dict(os.environ)
    for var in env_removals:
        env.pop(var, None)
    env.update(env_overrides)
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, path derived from __file__
        [sys.executable, "-m", "pytest", _FIXTURE_TARGET, "-v", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_explicit_url_with_a_broken_connect_is_refused_before_collection() -> None:
    """``TEST_DATABASE_URL`` set to an address that times out -> USAGE_ERROR, not a silent pass.

    This is the exact scenario the phaze-cpn5k spike measured as ``EXIT=0``: before this bead, the
    run below would have printed ``2 passed`` (the fixture module has two trivial tests) with only
    the word ``unlocked`` in its header to show anything was wrong. It must now refuse before a
    single test runs.
    """
    result = _run_subprocess_suite(env_overrides={"TEST_DATABASE_URL": _BLACKHOLE_DSN})
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, (
        f"an explicit, unreachable TEST_DATABASE_URL must refuse to run, got exit {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    output = result.stdout + result.stderr
    assert "Refusing to start" in output
    assert _BLACKHOLE_DSN in output, "the refusal must name the DSN that could not be reached"
    assert "TEST_DATABASE_URL" in output
    assert "just test-db" in output, "the refusal must name the remedy"
    # The two trivial tests in the fixture module must never have run.
    assert "2 passed" not in output
    assert "collected 2 item" not in output


def test_unset_url_with_postgres_unreachable_still_runs_the_db_free_tests() -> None:
    """No ``TEST_DATABASE_URL`` at all -> the default-URL fail-OPEN path is unchanged.

    Simulated by leaving ``TEST_DATABASE_URL`` unset (so ``explicit`` is ``False``) while pointing
    the module's OWN default at an unused local port via ``PHAZE_TEST_DB_PORT`` -- a fast,
    unambiguous ECONNREFUSED rather than a multi-second timeout, since this test only needs to
    prove the exemption survives, not re-measure the timeout. This pins the half of the phaze-34gkn,
    2026-09-16 operator decision that must NOT change: a bare ``uv run pytest`` with no harness up
    keeps running the thousands of DB-free tests.
    """
    result = _run_subprocess_suite(
        env_overrides={"PHAZE_TEST_DB_PORT": _UNUSED_LOCAL_PORT},
        env_removals=("TEST_DATABASE_URL",),
    )
    assert result.returncode == 0, (
        f"an unreachable DEFAULT database must still fail OPEN and run the DB-free tests, "
        f"got exit {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    output = result.stdout + result.stderr
    assert "unlocked (Postgres unreachable or bypass set)" in output
    assert "2 passed" in output
