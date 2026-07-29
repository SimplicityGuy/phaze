"""Single source of truth for resolving and validating the test-database DSN.

Every test module that touches a real Postgres resolves its target through this module, so
there is exactly ONE predicate deciding "is this a test database?" and exactly ONE way it can
fail. Before this module the predicate was copy-pasted into seven integration modules plus the
TRUNCATE guard in ``tests/integration/conftest.py``, each with its own message and each
``pytest.skip``-ing on failure.

Two distinct conditions, two DELIBERATELY different outcomes
------------------------------------------------------------

* **Postgres is not running** -> ``skip``. Not every contributor has the ephemeral harness up,
  and a bare ``uv run pytest`` should still run the ~3900 tests that need no database. Callers
  keep handling this themselves by probing connectivity; this module is not involved.

* **Postgres IS reachable but the target is misconfigured** -> ``error``. This is the case this
  module exists for. A misconfigured target means the run tested something other than what it
  claims to have tested, and a skip is indistinguishable from a pass in a pytest summary line.
  ``require_test_database`` therefore raises, and the run goes red.

Why the predicate accepts a ``_test`` SEGMENT, not just a suffix
---------------------------------------------------------------

The obvious way to make a per-worktree isolated database is to SUFFIX the standard name --
``phaze_test_<bead>`` -- and dozens of such databases already exist from earlier sessions. The
old ``name.endswith("_test")`` predicate rejected every one of them, and because rejection was a
skip, roughly 18 integration tests silently vanished from those runs while pytest still reported
green (measured: ``phaze_test_m7ya`` -> 3677 passed / 20 skipped, versus ``phaze_m7ya_test`` ->
3732 passed / 2 skipped -- 55 tests' difference, with nothing in the output to signal it).

Accepting ``(^|_)test(_|$)`` makes the established naming convention work as intended. This
relaxation is only safe BECAUSE the failure mode is now a hard error: a permissive predicate
combined with a silent skip would be the worst of both worlds, since it widens what slips
through while keeping the failure invisible. Permissive predicate + loud failure is the safe
pairing, and it is the pairing implemented here.

The name is only half the invariant: ONE database, ONE pytest process
-------------------------------------------------------------------

``TEST_DATABASE_URL`` isolates a *worktree*, not a *process*. Nothing stopped two pytest
processes resolving the SAME DSN, and two pytest processes on one database destroy each
other: ``tests/conftest.py``'s session-scoped ``async_engine`` runs ``Base.metadata.create_all``
at session start and ``drop_all`` at session teardown, so whichever process finishes FIRST
drops the schema out from under the other. Measured on 2026-07-29, two processes sharing
``phaze_ieqgx_test`` -- ``pytest tests/analyze/routers`` (61 tests, 6.8 s) and ``pytest tests/review``:
the short run's teardown left the long run at **238 failed + 12 errors**, every one of them
``UndefinedTableError: relation "agents" does not exist``, and every one green when re-run
alone. The shared harness Postgres log carries the server-side half of the same collision
from earlier runs -- ``DROP TABLE tracklists`` / ``DROP TABLE files`` deadlocking against live
``SELECT count(...)`` / ``INSERT INTO files`` inside a single test database.

That signature -- a large, branch-unrelated failure set that is green on isolated re-run --
is indistinguishable from the "concurrent worktrees contend on a shared surface" story, and
was misread as exactly that for two dispatch rounds (phaze-ieqg). It is not concurrency
between worktrees; it is concurrency between processes on one database. Postgres and Redis
were already isolated per worktree; the un-isolated surface was the *process count* per
database.

:func:`acquire_exclusive_session_lock` closes it with a session-level Postgres advisory lock
(advisory locks are per-database, so the DSN scopes the lock for free) taken in
``pytest_sessionstart`` and held for the whole run. A second process fails FAST, before
collection, with the remediation printed -- the same "loud, never silent" pairing as the
name predicate above.
"""

from __future__ import annotations

import contextlib
import os
import re
from typing import TYPE_CHECKING

from sqlalchemy.engine import make_url


if TYPE_CHECKING:
    import psycopg


# The ephemeral integration-test Postgres lives on 5433. The justfile reserves 5432 for the
# DEVELOPER's own database (justfile:4-5, whose comment says so explicitly), so 5432 must never
# be a default here: on a machine where 5432 holds a reachable phaze database with matching
# credentials, these fixtures' create/drop-schema cycle would run against live developer data.
# `tests/integration/test_migrations/conftest.py` already defaults to 5433 for this exact
# reason; this brings the main harness into line with it.
DEFAULT_TEST_DB_PORT = os.environ.get("PHAZE_TEST_DB_PORT", "5433")
DEFAULT_TEST_DATABASE_URL = f"postgresql+asyncpg://phaze:phaze@localhost:{DEFAULT_TEST_DB_PORT}/phaze_test"

# A ``test`` segment anywhere in the name: ``phaze_test``, ``phaze_test_m7ya``, ``phaze_m7ya_test``.
# Rejects ``phaze``, ``phaze_prod``, ``testing``, ``latest``, ``contest``.
_TEST_SEGMENT = re.compile(r"(?:^|_)test(?:_|$)")


# Advisory-lock key for "one pytest session owns this test database". Postgres advisory locks are
# scoped to the DATABASE, so a single constant key is already per-worktree isolated: two sessions
# collide only when they resolved the same DSN, which is exactly the condition being refused. The
# value is arbitrary but must stay stable across versions or an old and a new checkout would not
# see each other. It is deliberately far from the application's own advisory-lock keys
# (``5_000_504`` and ``hashtext(...)`` in src/phaze) so a test can never contend with product code.
TEST_DB_SESSION_LOCK_KEY = 0x7048415A_45544553  # 'pHAZETES' -- 8 bytes, fits int8

# Escape hatch. Deliberately awkward to reach for, and documented as NOT covering pytest-xdist:
# xdist workers against ONE database are precisely the create_all/drop_all race this lock exists
# to refuse (see .github/workflows/tests.yml, which keeps every DB bucket serial for that reason).
ALLOW_SHARED_ENV_VAR = "PHAZE_TEST_DB_ALLOW_SHARED"

# "Some OTHER backend IN THIS DATABASE is queued waiting on a lock" -- the deterministic barrier
# three concurrency modules use instead of guessing a sleep duration.
#
# phaze-ieqg: ``pg_locks`` is CLUSTER-wide. The three modules each carried the unqualified
# ``SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)``, which on the shared harness is
# satisfied by ANY concurrent worktree's blocked backend. The barrier then returns before this
# test's own waiter has queued, and the assertions that follow race -- a false red in a module the
# branch under test never touched, green on isolated re-run. Measured during the phaze-ieqg
# two-concurrent-suite proof.
#
# Filtered via ``pg_stat_activity.datname`` rather than ``pg_locks.database`` deliberately: the
# waiter these modules create blocks on a ``transactionid`` lock (a row-level ``FOR UPDATE``
# behind another transaction), and ``pg_locks.database`` is NULL for transaction-ID locks. A
# ``l.database = ...`` predicate would therefore never match and the barrier would always time
# out. The WAITING BACKEND's database is always populated, so join to it instead.
BLOCKED_WAITER_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid WHERE NOT l.granted AND a.datname = current_database())"
)


class NonTestDatabaseError(RuntimeError):
    """Raised when a destructive test fixture is pointed at a database that is not a test database.

    Deliberately an error and not a ``pytest.skip``: skipping downgrades verification silently,
    which is the exact defect this guard was introduced to remove.
    """


class SharedTestDatabaseError(RuntimeError):
    """Raised when a second pytest session tries to use a test database another session owns.

    Like :class:`NonTestDatabaseError` this is an error, never a skip or a wait: the two runs
    would silently corrupt each other's schema, and the victim's failure set looks like a real
    regression rather than a harness collision.
    """


def coerce_async_dsn(dsn: str) -> str:
    """Coerce a libpq / psycopg2 Postgres DSN to the asyncpg driver the async fixtures need.

    ``async_engine`` feeds ``TEST_DATABASE_URL`` straight to ``create_async_engine``. A bare
    ``postgresql://`` (or explicit ``postgresql+psycopg2://``) URL resolves SQLAlchemy's default
    ``psycopg2`` sync dialect, which is not installed (the async stack uses asyncpg) -- every
    DB-fixture test then dies at setup with a cryptic ``No module named 'psycopg2'``. Operators
    naturally export the libpq form (it matches ``PHAZE_QUEUE_URL``), so normalize it here rather
    than leaking the footgun into each fixture.
    """
    for sync_prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://"):
        if dsn.startswith(sync_prefix):
            return "postgresql+asyncpg://" + dsn[len(sync_prefix) :]
    return dsn


def is_test_database_name(name: str) -> bool:
    """Return whether ``name`` carries a ``test`` segment, and is therefore safe to wipe."""
    return bool(_TEST_SEGMENT.search(name))


def database_name(dsn: str) -> str:
    """Extract the database name from ``dsn``, or ``""`` when it carries none."""
    return make_url(dsn).database or ""


def resolve_test_dsn() -> str:
    """Return the async DSN the suite should target, defaulting to the 5433 test harness."""
    return coerce_async_dsn(os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL)


def integration_dsns() -> tuple[str, str]:
    """Return ``(broker_dsn, sa_dsn)`` for the real-Postgres integration harness.

    ``broker_dsn`` is the libpq form psycopg/SAQ want; ``sa_dsn`` is the asyncpg form SQLAlchemy
    wants. Both describe the SAME database. Six integration modules previously derived this pair
    with a copy-pasted expression whose fallback named database ``phaze`` -- not a test name at
    all. Under the old skip-on-mismatch guard that fallback quietly disabled the module; routing
    it through the shared resolver means one target, validated once.
    """
    sa_dsn = resolve_test_dsn()
    broker_dsn = os.environ.get("PHAZE_QUEUE_URL") or sa_dsn.replace("postgresql+asyncpg://", "postgresql://")
    return broker_dsn, sa_dsn


def libpq_dsn(dsn: str) -> str:
    """Return ``dsn`` in the plain libpq form psycopg understands (drops any ``+driver`` suffix)."""
    return make_url(dsn).set(drivername="postgresql").render_as_string(hide_password=False)


def session_lock_application_name() -> str:
    """Return the ``application_name`` this process stamps on its lock connection.

    It carries the OS pid so the refusal message can name the process still holding the database
    -- ``kill``-able, or at least identifiable in ``ps``. A bare "someone else has it" message
    sends the reader hunting; this one ends the hunt.
    """
    return f"phaze-pytest-session-lock pid={os.getpid()}"


def _lock_holder_description(conn: psycopg.Connection[tuple[object, ...]]) -> str:
    """Describe the backend currently holding the session lock, best-effort.

    Best-effort on purpose: the holder can disconnect between the failed ``pg_try_advisory_lock``
    and this query, and a diagnostic that raises would replace an actionable message with a
    traceback about the diagnostic.

    ``l.database`` is filtered even though the lock could only have been refused by a holder in
    OUR database: ``pg_locks`` is cluster-wide, so on the shared harness -- where every other seat
    holds this same key in ITS own database -- an unfiltered ``LIMIT 1`` would happily name an
    innocent seat's pid. A confidently wrong pid is worse than no pid.
    """
    try:
        row = conn.execute(
            """
            SELECT a.application_name, a.pid, a.backend_start
            FROM pg_locks l
            JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.locktype = 'advisory'
              AND l.granted
              AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND l.objsubid = 1
              AND ((l.classid::bigint << 32) | l.objid::bigint) = %s::bigint
            LIMIT 1
            """,
            (TEST_DB_SESSION_LOCK_KEY,),
        ).fetchone()
    except Exception:
        return "another pytest session (holder details unavailable)"
    if row is None:
        return "another pytest session (holder disconnected while we looked)"
    app_name, pid, backend_start = row
    return f"{app_name or 'an unnamed client'} (backend pid {pid}, connected {backend_start})"


def acquire_exclusive_session_lock(dsn: str, *, connect_timeout: int = 5) -> psycopg.Connection[tuple[object, ...]] | None:
    """Take the "one pytest session owns this database" advisory lock, or refuse to start.

    Returns the OPEN connection holding the lock -- the caller must keep it alive for the whole
    pytest session and pass it to :func:`release_exclusive_session_lock` at the end, because a
    session-level advisory lock lives on its connection.

    Three outcomes, matching this module's established shape:

    * **Postgres unreachable** -> ``None``, no lock, no complaint. A bare ``uv run pytest`` with no
      harness up must still run the thousands of DB-free tests; connectivity is a skip condition
      everywhere else in this module and stays one here.
    * **Lock free** -> acquired, connection returned.
    * **Lock held** -> :class:`SharedTestDatabaseError`. Never a wait: a run that blocks for the
      length of somebody else's suite is a run nobody will diagnose correctly.
    """
    if os.environ.get(ALLOW_SHARED_ENV_VAR):
        return None

    import psycopg  # imported lazily: DB-free runs should not pay for it, and it is a test-only dep

    try:
        conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(
            libpq_dsn(dsn),
            autocommit=True,  # a session-level advisory lock must not sit inside an open transaction
            connect_timeout=connect_timeout,
            application_name=session_lock_application_name(),
        )
    except psycopg.OperationalError:
        return None  # harness not up (or not this run's problem) -- see docstring

    try:
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (TEST_DB_SESSION_LOCK_KEY,)).fetchone()
        if row is not None and row[0]:
            return conn
        holder = _lock_holder_description(conn)
    except Exception:
        conn.close()
        raise

    name = database_name(dsn)
    conn.close()
    raise SharedTestDatabaseError(
        f"Refusing to start: test database {name!r} is already owned by {holder}.\n"
        f"\n"
        f"Two pytest processes on ONE database destroy each other. The session-scoped `async_engine`\n"
        f"fixture runs Base.metadata.create_all at session start and drop_all at session teardown, so\n"
        f"whichever process finishes FIRST drops the schema out from under the other. The victim then\n"
        f'reports hundreds of branch-unrelated failures (`relation "agents" does not exist`) that all\n'
        f"pass on an isolated re-run -- a false red indistinguishable from a real regression.\n"
        f"\n"
        f"Give this run its own database:\n"
        f"    just test-db-for <name>      # then export the three lines it prints\n"
        f"or wait for the other run to finish.\n"
        f"\n"
        f"{ALLOW_SHARED_ENV_VAR}=1 bypasses this check. Running pytest-xdist against one database is\n"
        f"this exact defect and is not a reason to set it."
    )


def release_exclusive_session_lock(conn: psycopg.Connection[tuple[object, ...]] | None) -> None:
    """Release the session lock and close its connection. Safe to call with ``None``.

    Closing alone would release the lock (Postgres drops session advisory locks with the backend),
    but the explicit unlock keeps the intent readable and makes the release deterministic even if
    something else is holding a reference to the connection.
    """
    if conn is None:
        return
    try:
        # Suppressed broadly on purpose: this runs in `pytest_sessionfinish`, after the last test.
        # An exception here (server already gone, connection reset by a `test-db-down`) would turn
        # an otherwise-green run red for a cleanup that `conn.close()` completes anyway.
        with contextlib.suppress(Exception):
            conn.execute("SELECT pg_advisory_unlock(%s)", (TEST_DB_SESSION_LOCK_KEY,))
    finally:
        conn.close()


def require_test_database(dsn: str, *, context: str) -> str:
    """Return the target database name, raising :class:`NonTestDatabaseError` if it is not a test DB.

    ``context`` names the caller ("orphan-count integration tests", "TRUNCATE") so the failure
    message says which run was refused.
    """
    name = database_name(dsn)
    if not is_test_database_name(name):
        raise NonTestDatabaseError(
            f"Refusing to run {context} against non-test database {name!r}. "
            f"The database name must contain a 'test' segment (e.g. 'phaze_test', 'phaze_test_<bead>', "
            f"'phaze_<bead>_test'). Set TEST_DATABASE_URL to a test DSN -- run `just test-db` for the "
            f"shared harness, or `just test-db-for <name>` for an isolated per-worktree pair. "
            f"This is an ERROR rather than a skip on purpose: a skipped guard silently drops integration "
            f"coverage while the run still reports green."
        )
    return name
