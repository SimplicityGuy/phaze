"""Tests for migration 018: create analysis_window table + query indexes.

Drives its own downgrade/upgrade sequence (mirroring test_017_upgrade.py /
test_downgrade.py) rather than the ``migrated_engine`` fixture. The test:

  - upgrades to 017 (analysis_window does NOT exist),
  - upgrades to 018 and asserts the table exists, the FK is ON DELETE CASCADE,
    and the composite + partial + label indexes exist,
  - asserts deleting a file cascades to its analysis_window rows (no orphans),
  - downgrades to 017 and asserts the table is dropped,
  - cleans up via downgrade_to('base') in a finally block.

Static-string assertions (revision/down_revision) run without a DB so the
additive-only / bare-number contract is checked even where Postgres is absent.

Operator pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` (see ``tests/test_migrations/conftest.py``).
"""

import asyncio
import importlib.util
from pathlib import Path
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


def _load_migration_018() -> object:
    """Load the 018 migration module by path (its name starts with a digit)."""
    migration_path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "018_add_analysis_window_table.py"
    spec = importlib.util.spec_from_file_location("migration_018", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_TABLE_EXISTS_SQL = "SELECT to_regclass('analysis_window') AS t"
_INDEX_NAMES_SQL = "SELECT indexname FROM pg_indexes WHERE tablename = 'analysis_window'"
_FK_DELETE_RULE_SQL = (
    "SELECT rc.delete_rule "
    "FROM information_schema.referential_constraints rc "
    "JOIN information_schema.table_constraints tc "
    "  ON rc.constraint_name = tc.constraint_name "
    "WHERE tc.table_name = 'analysis_window' AND tc.constraint_type = 'FOREIGN KEY'"
)

# A minimal files row (FK target). agent_id 'legacy-application-server' is created by migration 012's backfill.
_INSERT_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :hash, :path, :name, :path, 'mp3', 1000, 'discovered', NOW(), NOW())"
)
_INSERT_WINDOW_SQL = (
    "INSERT INTO analysis_window (id, file_id, tier, window_index, start_sec, end_sec, bpm, created_at, updated_at) "
    "VALUES (:id, :file_id, 'fine', 0, 0.0, 30.0, 128.0, NOW(), NOW())"
)


def test_revision_identifiers_are_bare_numbers() -> None:
    """018 chains off 017 using bare-number strings (no long migration names)."""
    migration_018 = _load_migration_018()
    assert migration_018.revision == "018"  # type: ignore[attr-defined]
    assert migration_018.down_revision == "017"  # type: ignore[attr-defined]
    assert migration_018.branch_labels is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_upgrade_018_creates_table_indexes_and_cascade() -> None:
    """018 creates analysis_window with CASCADE FK + composite/partial/label indexes; delete cascades."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "017")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: table absent at revision 017.
        async with engine.connect() as conn:
            exists_before = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_before is None, "analysis_window must not exist before migration 018"

        await asyncio.to_thread(upgrade_to, cfg, "018")

        async with engine.connect() as conn:
            exists_after = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_after is not None, "analysis_window must exist after migration 018"

            index_names = {r.indexname for r in (await conn.execute(text(_INDEX_NAMES_SQL))).all()}
            assert "ix_analysis_window_file_tier_idx" in index_names
            assert "ix_analysis_window_bpm_fine" in index_names
            assert "ix_analysis_window_dance_coarse" in index_names
            assert "ix_analysis_window_mood" in index_names
            assert "ix_analysis_window_style" in index_names

            delete_rule = (await conn.execute(text(_FK_DELETE_RULE_SQL))).scalar_one()
            assert delete_rule == "CASCADE", "file_id FK must be ON DELETE CASCADE"

        # Integrity: deleting a file cascades to its analysis_window rows (no orphans).
        file_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(text(_INSERT_FILE_SQL), {"id": file_id, "hash": "c" * 64, "path": "/music/w.mp3", "name": "w.mp3"})
            await conn.execute(text(_INSERT_WINDOW_SQL), {"id": uuid.uuid4(), "file_id": file_id})

        async with engine.connect() as conn:
            before = (await conn.execute(text("SELECT count(*) FROM analysis_window WHERE file_id = :f"), {"f": file_id})).scalar_one()
            assert before == 1

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM files WHERE id = :f"), {"f": file_id})

        async with engine.connect() as conn:
            after = (await conn.execute(text("SELECT count(*) FROM analysis_window WHERE file_id = :f"), {"f": file_id})).scalar_one()
            assert after == 0, "deleting a file must cascade-delete its analysis_window rows"

        # Downgrade drops the table.
        await asyncio.to_thread(downgrade_to, cfg, "017")
        async with engine.connect() as conn:
            exists_post_downgrade = (await conn.execute(text(_TABLE_EXISTS_SQL))).scalar_one()
            assert exists_post_downgrade is None, "downgrade to 017 must drop analysis_window"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
