"""Tests for migration 017: add scan_batches.last_progress_at + backfill from updated_at.

Drives its own downgrade/upgrade sequence (mirroring test_016_upgrade.py) rather
than the ``migrated_engine`` fixture (which always upgrades to head). The test:

  - downgrades to base, upgrades to 016 (last_progress_at column does NOT exist),
  - inserts a RUNNING row and a COMPLETED row, each with a known updated_at and a
    NULL (absent) last_progress_at,
  - upgrades to 017 and asserts the column now exists and every row's
    last_progress_at was backfilled to its updated_at,
  - downgrades to 016 and asserts the column is dropped,
  - cleans up via downgrade_to('base') in a finally block.

Operator pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` (see ``tests/test_migrations/conftest.py``).
"""

import asyncio
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


# The legacy agent id every scan_batches row FK-references (model-level default).
_LEGACY_AGENT_ID = "legacy-application-server"

# Insert at revision 016, where last_progress_at does NOT yet exist.
_INSERT_BATCH_SQL = (
    "INSERT INTO scan_batches "
    "(id, agent_id, scan_path, status, total_files, processed_files, created_at, updated_at) "
    "VALUES (:id, :agent_id, :scan_path, :status, 0, 0, :created_at, :updated_at)"
)

_COLUMN_EXISTS_SQL = "SELECT 1 FROM information_schema.columns WHERE table_name = 'scan_batches' AND column_name = 'last_progress_at'"


@pytest.mark.asyncio
async def test_upgrade_017_backfills_last_progress_at_from_updated_at() -> None:
    """017 adds last_progress_at and backfills every NULL row to its updated_at."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "016")

    now = datetime.now(UTC)
    running_id = uuid.uuid4()
    completed_id = uuid.uuid4()
    running_updated = now - timedelta(minutes=5)
    completed_updated = now - timedelta(minutes=15)

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            # FK target ``legacy-application-server`` is created by migration 012's backfill.
            await conn.execute(
                text(_INSERT_BATCH_SQL),
                {
                    "id": running_id,
                    "agent_id": _LEGACY_AGENT_ID,
                    "scan_path": "/music/running",
                    "status": "running",
                    "created_at": running_updated - timedelta(minutes=1),
                    "updated_at": running_updated,
                },
            )
            await conn.execute(
                text(_INSERT_BATCH_SQL),
                {
                    "id": completed_id,
                    "agent_id": _LEGACY_AGENT_ID,
                    "scan_path": "/music/completed",
                    "status": "completed",
                    "created_at": completed_updated - timedelta(minutes=1),
                    "updated_at": completed_updated,
                },
            )

            # Pre-condition: the column does NOT exist at revision 016.
            exists_before = (await conn.execute(text(_COLUMN_EXISTS_SQL))).first()
            assert exists_before is None, "last_progress_at must not exist before migration 017"

        # Apply the add-column + backfill.
        await asyncio.to_thread(upgrade_to, cfg, "017")

        async with engine.connect() as conn:
            exists_after = (await conn.execute(text(_COLUMN_EXISTS_SQL))).first()
            assert exists_after is not None, "last_progress_at must exist after migration 017"

            rows = {
                r.id: r.last_progress_at
                for r in (
                    await conn.execute(
                        text("SELECT id, last_progress_at FROM scan_batches WHERE agent_id = :a"),
                        {"a": _LEGACY_AGENT_ID},
                    )
                ).all()
            }

        # Every row was backfilled to its updated_at (compare instants; asyncpg
        # returns tz-aware datetimes). RUNNING is NOT exempt from the backfill --
        # unlike completed_at, last_progress_at applies to in-flight scans too.
        assert rows[running_id] is not None
        assert rows[running_id] == running_updated
        assert rows[completed_id] is not None
        assert rows[completed_id] == completed_updated

        # Downgrade must drop the column.
        await asyncio.to_thread(downgrade_to, cfg, "016")
        async with engine.connect() as conn:
            exists_post_downgrade = (await conn.execute(text(_COLUMN_EXISTS_SQL))).first()
            assert exists_post_downgrade is None, "downgrade to 016 must drop last_progress_at"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
