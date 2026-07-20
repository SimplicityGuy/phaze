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
"""

from __future__ import annotations

import os
import re

from sqlalchemy.engine import make_url


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


class NonTestDatabaseError(RuntimeError):
    """Raised when a destructive test fixture is pointed at a database that is not a test database.

    Deliberately an error and not a ``pytest.skip``: skipping downgrades verification silently,
    which is the exact defect this guard was introduced to remove.
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
