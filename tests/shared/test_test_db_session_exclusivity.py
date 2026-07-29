"""Guard: ONE pytest process per test database (phaze-ieqg).

``TEST_DATABASE_URL`` isolates a worktree. It never isolated a *process*, and for two dispatch
rounds that gap was misread as "concurrent worktrees contend on an unknown third shared surface".
They do not -- Postgres and Redis are isolated per worktree already. What was unisolated was the
number of pytest processes pointed at one database.

THE COLLISION, measured 2026-07-29
----------------------------------

Two processes were started against the same ``phaze_ieqgx_test``::

    uv run pytest tests/analyze/routers   # 61 tests,  6.8s
    uv run pytest tests/review            # 711 tests, ~100s

The short run reached session teardown first. ``tests/conftest.py``'s session-scoped
``async_engine`` fixture ends in ``Base.metadata.drop_all``, so it deleted the schema out from
under the long run, which finished **238 failed, 473 passed, 12 errors** -- every failure a
variant of ``asyncpg.exceptions.UndefinedTableError: relation "agents" does not exist``, and
every one green when re-run alone. The shared harness Postgres log carries the server side of the
same collision from earlier runs: ``DROP TABLE tracklists`` / ``DROP TABLE files`` deadlocking
against live ``SELECT count(...)`` / ``INSERT INTO files`` within a single test database.

WHY THIS SHAPE OF BUG IS EXPENSIVE
----------------------------------

The victim's failure set is large, spread across modules the branch under test never touched, and
green on isolated re-run. That is indistinguishable by inspection from "a real regression plus a
flaky harness", so reviewers learn to dismiss red runs -- the same trained-to-ignore-red failure
mode ``tests/shared/test_redis_worktree_isolation.py`` was written to prevent for Redis.

THE GUARD
---------

``tests/db_guard.acquire_exclusive_session_lock`` takes a session-level Postgres advisory lock in
``pytest_sessionstart`` and holds it for the whole run. Advisory locks are scoped to the database,
so the DSN scopes the lock for free: two worktrees with different databases never see each other,
two processes with the same database always do. The second process fails before collection with
the remediation printed.

These tests are self-referential on purpose: the running pytest session IS the lock holder, so
asserting that a second acquisition is refused proves the guard is live in this very run, not
merely that the helper compiles.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import _SESSION_LOCK_ATTR, TEST_DATABASE_URL
from tests.db_guard import (
    ALLOW_SHARED_ENV_VAR,
    TEST_DB_SESSION_LOCK_KEY,
    SharedTestDatabaseError,
    acquire_exclusive_session_lock,
    database_name,
    libpq_dsn,
    release_exclusive_session_lock,
    session_lock_application_name,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _require_live_lock(pytestconfig: pytest.Config) -> None:
    """Skip when this session holds no lock -- i.e. Postgres is not up (a skip everywhere else too)."""
    if getattr(pytestconfig, _SESSION_LOCK_ATTR, None) is None:
        pytest.skip("no test-database lock held (Postgres not reachable); nothing to assert against")


def test_this_session_holds_the_exclusive_lock(pytestconfig: pytest.Config) -> None:
    """``pytest_sessionstart`` actually wired the guard: a live lock connection is stashed on config.

    Asserted separately from the refusal test below so a wiring regression (hook renamed, import
    dropped) is distinguishable from a lock-semantics regression.
    """
    _require_live_lock(pytestconfig)
    lock = getattr(pytestconfig, _SESSION_LOCK_ATTR)
    assert not lock.closed, "the session lock connection must stay open for the whole run"


def test_a_second_session_is_refused_while_this_one_holds_the_lock(pytestconfig: pytest.Config) -> None:
    """A second acquisition against the same DSN raises instead of proceeding.

    This is the whole bug in one assertion: before the guard, the second acquisition here was a
    second pytest process, and it silently proceeded to share -- and then drop -- this session's
    schema.
    """
    _require_live_lock(pytestconfig)
    with pytest.raises(SharedTestDatabaseError) as excinfo:
        acquire_exclusive_session_lock(TEST_DATABASE_URL)
    assert database_name(TEST_DATABASE_URL) in str(excinfo.value)


def test_the_refusal_names_the_holder_and_the_remedy(pytestconfig: pytest.Config) -> None:
    """The message must identify WHO holds the database and WHAT to run -- not just that it is busy.

    "Database in use" without a pid sends the reader hunting through ``ps``; without
    ``just test-db-for`` it sends them to the justfile. Both belong in the one line they will read.
    """
    _require_live_lock(pytestconfig)
    with pytest.raises(SharedTestDatabaseError) as excinfo:
        acquire_exclusive_session_lock(TEST_DATABASE_URL)
    message = str(excinfo.value)
    assert session_lock_application_name() in message, "the refusal must name the holding process"
    assert "just test-db-for" in message, "the refusal must name the command that fixes it"
    assert ALLOW_SHARED_ENV_VAR in message, "the refusal must document its own escape hatch"


def test_the_refusal_ignores_holders_of_the_same_key_in_other_databases(pytestconfig: pytest.Config) -> None:
    """A holder in a DIFFERENT database must not be misreported as the one blocking this run.

    ``pg_locks`` is cluster-wide, and on the shared harness every other seat holds this very key
    against its own database, so an unfiltered holder lookup names an innocent seat's pid roughly
    as often as the real one. A confidently wrong pid is worse than no pid: it sends the operator
    to interrupt a run that was never involved.

    Simulated by taking the same key in the maintenance ``postgres`` database, which no seat ever
    tests against.
    """
    _require_live_lock(pytestconfig)
    import psycopg
    from sqlalchemy.engine import make_url

    decoy_dsn = make_url(libpq_dsn(TEST_DATABASE_URL)).set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(decoy_dsn, autocommit=True, connect_timeout=5, application_name="decoy-holder-other-database") as decoy:
        assert decoy.execute("SELECT pg_try_advisory_lock(%s)", (TEST_DB_SESSION_LOCK_KEY,)).fetchone()[0]  # type: ignore[index]
        try:
            with pytest.raises(SharedTestDatabaseError) as excinfo:
                acquire_exclusive_session_lock(TEST_DATABASE_URL)
        finally:
            decoy.execute("SELECT pg_advisory_unlock(%s)", (TEST_DB_SESSION_LOCK_KEY,))
    message = str(excinfo.value)
    assert session_lock_application_name() in message, "the refusal must name the real holder"
    assert "decoy-holder-other-database" not in message, "a holder in another database must never be blamed"


def test_a_real_second_pytest_process_is_refused_and_collect_only_is_not(pytestconfig: pytest.Config) -> None:
    """End to end, through an actual second ``pytest`` process, against the live lock this run holds.

    Every other test here calls the helper in-process, which proves the lock semantics but not the
    wiring: a ``pytest_sessionstart`` that stopped being called would leave them all passing. This
    one spawns the real thing.

    It also pins the ``--collect-only`` exemption. Collection imports modules and never opens the
    schema, so it cannot corrupt a live run -- and it is the one introspection command someone
    needs precisely WHILE a suite is running. Refusing it would be safety theatre.
    """
    _require_live_lock(pytestconfig)
    import subprocess
    import sys

    target = str(Path(__file__).relative_to(REPO_ROOT))

    collect = subprocess.run(  # noqa: S603 -- fixed argv, no shell, path derived from __file__
        [sys.executable, "-m", "pytest", target, "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert collect.returncode == 0, f"--collect-only must be allowed while a suite holds the lock:\n{collect.stdout}{collect.stderr}"

    run = subprocess.run(  # noqa: S603 -- fixed argv, no shell, path derived from __file__
        [sys.executable, "-m", "pytest", target, "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert run.returncode == pytest.ExitCode.USAGE_ERROR, f"a second real pytest process must be refused, got {run.returncode}"
    assert "Refusing to start" in run.stdout + run.stderr
    assert session_lock_application_name() in run.stdout + run.stderr, "the refusal must name THIS session as the holder"


def test_unreachable_postgres_yields_no_lock_rather_than_an_error() -> None:
    """No harness up -> ``None``, silently. A DB-free ``uv run pytest`` must still run.

    Port 1 is reserved and never listening, so this refuses the TCP connection immediately rather
    than waiting out ``connect_timeout``.
    """
    assert acquire_exclusive_session_lock("postgresql+asyncpg://phaze:phaze@localhost:1/phaze_unreachable_test") is None


def test_bypass_env_var_disables_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented escape hatch short-circuits before any connection is attempted.

    Checked against a DSN that WOULD be refused (this session owns it), so a pass proves the
    bypass, not merely that some DSN was free.
    """
    monkeypatch.setenv(ALLOW_SHARED_ENV_VAR, "1")
    assert acquire_exclusive_session_lock(TEST_DATABASE_URL) is None


def test_release_tolerates_no_lock() -> None:
    """Teardown runs unconditionally, including on the unreachable-Postgres path that returned ``None``."""
    release_exclusive_session_lock(None)


@pytest.mark.parametrize(
    ("dsn", "expected_prefix"),
    [
        ("postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test", "postgresql://"),
        ("postgresql+psycopg://phaze:phaze@localhost:5433/phaze_test", "postgresql://"),
        ("postgresql://phaze:phaze@localhost:5433/phaze_test", "postgresql://"),
    ],
)
def test_libpq_dsn_strips_the_sqlalchemy_driver_suffix(dsn: str, expected_prefix: str) -> None:
    """psycopg cannot parse ``postgresql+asyncpg://``; the lock connection must get the libpq form.

    The password must survive the rewrite -- ``render_as_string`` hides it by default, and a
    silently password-less DSN would degrade every run to the "Postgres unreachable" path and
    disable the guard without saying so.
    """
    rendered = libpq_dsn(dsn)
    assert rendered.startswith(expected_prefix)
    assert "+" not in rendered.split("://", 1)[0]
    assert "phaze:phaze@" in rendered
    assert rendered.endswith("/phaze_test")
