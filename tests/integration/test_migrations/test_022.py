"""Tests for migration 022: create + drop scheduling_ledger table (Phase 45 Plan 01).

Mirrors ``test_020.py``: static revision-id assertions run WITHOUT a DB so the
additive-only / bare-number contract is checked even where Postgres is absent; the
integration body drives its own downgrade/upgrade sequence against the ephemeral
migrations DB to prove the table is created at 022 and dropped on downgrade to 021.

CRITICAL: migration 022 must NEVER reference ``saq_jobs`` (SAQ owns that table via
``init_db()``). A grep-style assertion enforces this.

Operator pre-condition for the integration body: the database
``phaze_migrations_test`` must exist (see ``tests/test_migrations/conftest.py``);
run via ``just integration-test`` / ``just test-db``.
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "022_add_scheduling_ledger.py"


def _load_migration_022() -> object:
    """Load the 022 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_022", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_TABLE_EXISTS_SQL = "SELECT to_regclass('scheduling_ledger') AS t"


def test_revision_identifiers_are_bare_numbers() -> None:
    """022 chains off 021 using bare-number strings (no long migration names)."""
    migration_022 = _load_migration_022()
    assert migration_022.revision == "022"  # type: ignore[attr-defined]
    assert migration_022.down_revision == "021"  # type: ignore[attr-defined]
    assert migration_022.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    # Exclude the CRITICAL banner comment lines that name saq_jobs only to forbid it.
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 022 must not reference saq_jobs outside its banner: {offending}"


@pytest.mark.asyncio
async def test_upgrade_022_creates_then_downgrade_drops() -> None:
    """022 creates scheduling_ledger; downgrade to 021 drops it (reversible round-trip)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "021")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: table absent at revision 021.
        async with engine.connect() as conn:
            exists_before = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_before is None, "scheduling_ledger must not exist before migration 022"

        await asyncio.to_thread(upgrade_to, cfg, "022")

        async with engine.connect() as conn:
            exists_after = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_after is not None, "scheduling_ledger must exist after migration 022"

            # A bound INSERT round-trip lands a row keyed by ``key``.
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO scheduling_ledger (key, function, routing, payload, enqueued_at, created_at, updated_at) "
                        "VALUES (:key, :fn, :routing, :payload, NOW(), NOW(), NOW())"
                    ),
                    {"key": "process_file:abc", "fn": "process_file", "routing": "agent", "payload": "{}"},
                )
            count = (await conn.execute(text("SELECT count(*) FROM scheduling_ledger"))).scalar_one()
            assert count == 1

        # Downgrade drops the table.
        await asyncio.to_thread(downgrade_to, cfg, "021")
        async with engine.connect() as conn:
            exists_post_downgrade = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_post_downgrade is None, "downgrade to 021 must drop scheduling_ledger"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
