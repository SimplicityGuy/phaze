"""Tests for migration 015: scan_batches.completed_at nullable column (incident 260608).

Covers:
  - scan_batches.completed_at exists and is nullable after head (015) upgrade
  - the column type is a TIMESTAMP WITH TIME ZONE (tz-aware) to match the
    runtime behavior of TimestampMixin's columns

Operator pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` (see ``tests/test_migrations/conftest.py``).
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_scan_batches_completed_at_exists_and_nullable(migrated_engine) -> None:  # type: ignore[no-untyped-def]
    """015: scan_batches.completed_at is present, nullable, and tz-aware after head upgrade."""
    async with migrated_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT is_nullable, data_type FROM information_schema.columns WHERE table_name = 'scan_batches' AND column_name = 'completed_at'")
        )
        row = result.one()
    assert row.is_nullable == "YES"
    assert row.data_type == "timestamp with time zone"
