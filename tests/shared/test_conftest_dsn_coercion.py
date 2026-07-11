"""Regression guard for the ``TEST_DATABASE_URL`` async-driver coercion in ``tests/conftest.py``.

A bare ``postgresql://`` (or ``postgresql+psycopg2://``) DSN fed to ``create_async_engine`` resolves
SQLAlchemy's default psycopg2 sync dialect, which is not installed — every DB-fixture test then dies
at setup with ``No module named 'psycopg2'``. ``_coerce_async_dsn`` normalizes sync DSNs to asyncpg so
an operator can export the libpq form (matching ``PHAZE_QUEUE_URL``) without breaking the suite.
"""

from __future__ import annotations

import pytest

from tests.conftest import _coerce_async_dsn


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # Bare libpq DSN (the form operators naturally export, matching PHAZE_QUEUE_URL).
        (
            "postgresql://phaze:phaze@localhost:5433/phaze_test",
            "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test",
        ),
        # Explicit psycopg2 sync driver.
        (
            "postgresql+psycopg2://phaze:phaze@localhost:5433/phaze_test",
            "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test",
        ),
        # Explicit psycopg (v3) sync driver.
        (
            "postgresql+psycopg://phaze:phaze@localhost:5433/phaze_test",
            "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test",
        ),
        # Already-async DSN passes through untouched.
        (
            "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test",
            "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test",
        ),
    ],
)
def test_coerce_async_dsn_normalizes_sync_drivers(given: str, expected: str) -> None:
    assert _coerce_async_dsn(given) == expected


def test_coerce_async_dsn_only_rewrites_the_scheme_prefix() -> None:
    """The rewrite must touch only the leading driver token, not a psql substring in the db name."""
    given = "postgresql://user:pw@host:5432/postgresql_metrics"
    assert _coerce_async_dsn(given) == "postgresql+asyncpg://user:pw@host:5432/postgresql_metrics"
