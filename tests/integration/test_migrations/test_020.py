"""Tests for migration 020: create + seed pipeline_stage_control table.

Drives its own downgrade/upgrade sequence (mirroring test_migration_018.py)
rather than the ``migrated_engine`` fixture. The test:

  - upgrades to 019 (pipeline_stage_control does NOT exist),
  - upgrades to 020 and asserts the table exists with exactly 3 seeded rows
    (metadata/analyze/fingerprint), all paused=false / priority=50,
  - asserts the CHECK rejects a priority outside 0..100,
  - downgrades to 019 and asserts the table is dropped,
  - cleans up via downgrade_to('base') in a finally block.

Static-string assertions (revision/down_revision) run without a DB so the
additive-only / bare-number contract is checked even where Postgres is absent.

Operator pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` (see ``tests/test_migrations/conftest.py``); run via
``just integration-test`` / ``just test-db``.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


def _load_migration_020() -> object:
    """Load the 020 migration module by path (its name starts with a digit)."""
    migration_path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "020_add_pipeline_stage_control.py"
    spec = importlib.util.spec_from_file_location("migration_020", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_TABLE_EXISTS_SQL = "SELECT to_regclass('pipeline_stage_control') AS t"
_COUNT_SQL = "SELECT count(*) FROM pipeline_stage_control"
_ROWS_SQL = "SELECT stage, paused, priority FROM pipeline_stage_control ORDER BY stage"
_EXPECTED_STAGES = {"metadata", "analyze", "fingerprint"}


def test_revision_identifiers_are_bare_numbers() -> None:
    """020 chains off 019 using bare-number strings (no long migration names)."""
    migration_020 = _load_migration_020()
    assert migration_020.revision == "020"  # type: ignore[attr-defined]
    assert migration_020.down_revision == "019"  # type: ignore[attr-defined]
    assert migration_020.branch_labels is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upgrade_020_creates_seeds_and_check_then_downgrade_drops() -> None:
    """020 creates pipeline_stage_control, seeds 3 rows, enforces the priority CHECK, downgrade drops it."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "019")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: table absent at revision 019.
        async with engine.connect() as conn:
            exists_before = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_before is None, "pipeline_stage_control must not exist before migration 020"

        await asyncio.to_thread(upgrade_to, cfg, "020")

        async with engine.connect() as conn:
            exists_after = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_after is not None, "pipeline_stage_control must exist after migration 020"

            count = (await conn.execute(text(_COUNT_SQL))).scalar_one()
            assert count == 3, "migration 020 must seed exactly 3 rows"

            rows = (await conn.execute(text(_ROWS_SQL))).all()
            assert {r.stage for r in rows} == _EXPECTED_STAGES
            for r in rows:
                assert r.paused is False, f"stage {r.stage} must seed paused=false"
                assert r.priority == 50, f"stage {r.stage} must seed priority=50"

        # CHECK: a write with priority outside 0..100 must raise a CHECK/IntegrityError.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE pipeline_stage_control SET priority = 200 WHERE stage = 'analyze'"))

        # Downgrade drops the table.
        await asyncio.to_thread(downgrade_to, cfg, "019")
        async with engine.connect() as conn:
            exists_post_downgrade = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_post_downgrade is None, "downgrade to 019 must drop pipeline_stage_control"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
