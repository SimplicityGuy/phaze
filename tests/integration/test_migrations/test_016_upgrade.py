"""Tests for migration 016: backfill scan_batches.completed_at on terminal NULL rows.

Drives its own downgrade/upgrade sequence (mirroring test_downgrade.py) rather
than using the ``migrated_engine`` fixture (which always upgrades to head). The
test:

  - downgrades to base, upgrades to 015 (completed_at column exists, no backfill),
  - inserts four scan_batches rows under the legacy agent with known updated_at:
      * COMPLETED + completed_at NULL  -> must be backfilled to updated_at
      * FAILED    + completed_at NULL  -> must be backfilled to updated_at
      * RUNNING   + completed_at NULL  -> must stay NULL (non-terminal)
      * COMPLETED + completed_at SET   -> must keep its original timestamp
  - upgrades to 016 and asserts the four post-conditions,
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

_INSERT_BATCH_SQL = (
    "INSERT INTO scan_batches "
    "(id, agent_id, scan_path, status, total_files, processed_files, completed_at, created_at, updated_at) "
    "VALUES (:id, :agent_id, :scan_path, :status, 0, 0, :completed_at, :created_at, :updated_at)"
)


@pytest.mark.asyncio
async def test_upgrade_016_backfills_only_terminal_null_rows() -> None:
    """016 sets completed_at=updated_at for terminal NULL rows; leaves others untouched."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "015")

    now = datetime.now(UTC)
    completed_null_id = uuid.uuid4()
    failed_null_id = uuid.uuid4()
    running_null_id = uuid.uuid4()
    completed_set_id = uuid.uuid4()

    # Distinct updated_at per row so the backfill target is unambiguous.
    completed_updated = now - timedelta(minutes=10)
    failed_updated = now - timedelta(minutes=20)
    running_updated = now - timedelta(minutes=5)
    # Already-stamped row: completed_at distinct from updated_at, must survive intact.
    already_completed_at = now - timedelta(minutes=30)
    already_updated = now - timedelta(minutes=29)

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            # FK target ``legacy-application-server`` is created by migration 012's
            # backfill, so no agent insert is needed here.
            await conn.execute(
                text(_INSERT_BATCH_SQL),
                {
                    "id": completed_null_id,
                    "agent_id": _LEGACY_AGENT_ID,
                    "scan_path": "/music/completed",
                    "status": "completed",
                    "completed_at": None,
                    "created_at": completed_updated - timedelta(minutes=1),
                    "updated_at": completed_updated,
                },
            )
            await conn.execute(
                text(_INSERT_BATCH_SQL),
                {
                    "id": failed_null_id,
                    "agent_id": _LEGACY_AGENT_ID,
                    "scan_path": "/music/failed",
                    "status": "failed",
                    "completed_at": None,
                    "created_at": failed_updated - timedelta(minutes=1),
                    "updated_at": failed_updated,
                },
            )
            await conn.execute(
                text(_INSERT_BATCH_SQL),
                {
                    "id": running_null_id,
                    "agent_id": _LEGACY_AGENT_ID,
                    "scan_path": "/music/running",
                    "status": "running",
                    "completed_at": None,
                    "created_at": running_updated - timedelta(minutes=1),
                    "updated_at": running_updated,
                },
            )
            await conn.execute(
                text(_INSERT_BATCH_SQL),
                {
                    "id": completed_set_id,
                    "agent_id": _LEGACY_AGENT_ID,
                    "scan_path": "/music/already",
                    "status": "completed",
                    "completed_at": already_completed_at,
                    "created_at": already_updated - timedelta(minutes=1),
                    "updated_at": already_updated,
                },
            )

        # Apply the backfill.
        await asyncio.to_thread(upgrade_to, cfg, "016")

        async with engine.connect() as conn:
            rows = {
                r.id: r.completed_at
                for r in (
                    await conn.execute(
                        text("SELECT id, completed_at FROM scan_batches WHERE agent_id = :a"),
                        {"a": _LEGACY_AGENT_ID},
                    )
                ).all()
            }

        # Terminal NULL rows are backfilled to their updated_at (compare instants;
        # asyncpg returns tz-aware datetimes).
        assert rows[completed_null_id] is not None
        assert rows[completed_null_id] == completed_updated
        assert rows[failed_null_id] is not None
        assert rows[failed_null_id] == failed_updated
        # Non-terminal RUNNING row is untouched.
        assert rows[running_null_id] is None
        # Already-stamped terminal row keeps its original completed_at (not clobbered).
        assert rows[completed_set_id] == already_completed_at
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
