"""Tests for migration 023: add timeout + retries columns to scheduling_ledger.

Mirrors ``test_022.py``: static revision-id assertions run WITHOUT a DB so the
additive-only / bare-number contract holds even where Postgres is absent; the integration
body drives its own downgrade/upgrade sequence against the ephemeral migrations DB to prove
the two columns exist after 023 and are dropped on downgrade to 022.

CRITICAL: migration 023 must NEVER reference ``saq_jobs`` (SAQ owns that table). A grep-style
assertion enforces this.

Operator pre-condition for the integration body: the database ``phaze_migrations_test`` must
exist (see ``tests/test_migrations/conftest.py``); run via ``just integration-test`` /
``just test-db``.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "023_add_scheduling_ledger_job_policy.py"


def _load_migration_023() -> object:
    """Load the 023 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_023", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_COLUMNS_SQL = (
    "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
    "WHERE table_name = 'scheduling_ledger' AND column_name IN ('timeout', 'retries') ORDER BY column_name"
)


def test_revision_identifiers_are_bare_numbers() -> None:
    """023 chains off 022 using bare-number strings (no long migration names)."""
    migration_023 = _load_migration_023()
    assert migration_023.revision == "023"  # type: ignore[attr-defined]
    assert migration_023.down_revision == "022"  # type: ignore[attr-defined]
    assert migration_023.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 023 must not reference saq_jobs outside its banner: {offending}"


@pytest.mark.asyncio
async def test_upgrade_023_adds_columns_then_downgrade_drops() -> None:
    """023 adds nullable timeout/retries; downgrade to 022 drops them (reversible round-trip)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "022")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: columns absent at revision 022.
        async with engine.connect() as conn:
            cols_before = (await conn.execute(text(_COLUMNS_SQL))).all()
            assert cols_before == [], "timeout/retries must not exist before migration 023"

        await asyncio.to_thread(upgrade_to, cfg, "023")

        async with engine.connect() as conn:
            cols_after = {row[0]: (row[1], row[2]) for row in (await conn.execute(text(_COLUMNS_SQL))).all()}
            assert set(cols_after) == {"retries", "timeout"}, f"both columns must exist after 023: {cols_after}"
            # Both nullable integers so a legacy/no-policy row stays NULL.
            assert cols_after["timeout"] == ("YES", "integer")
            assert cols_after["retries"] == ("YES", "integer")

            # A row carrying an explicit policy round-trips.
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO scheduling_ledger (key, function, routing, payload, timeout, retries, enqueued_at, created_at, updated_at) "
                        "VALUES (:key, :fn, :routing, :payload, :timeout, :retries, NOW(), NOW(), NOW())"
                    ),
                    {"key": "process_file:xyz", "fn": "process_file", "routing": "agent", "payload": "{}", "timeout": 7200, "retries": 2},
                )
            row = (await conn.execute(text("SELECT timeout, retries FROM scheduling_ledger WHERE key = 'process_file:xyz'"))).one()
            assert row == (7200, 2)

        # Downgrade drops both columns.
        await asyncio.to_thread(downgrade_to, cfg, "022")
        async with engine.connect() as conn:
            cols_post_downgrade = (await conn.execute(text(_COLUMNS_SQL))).all()
            assert cols_post_downgrade == [], "downgrade to 022 must drop timeout/retries"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
