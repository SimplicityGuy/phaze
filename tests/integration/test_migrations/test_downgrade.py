"""Tests for migration downgrade paths: clean roundtrip and D-16 dupe-detection error.

Covers VALIDATION.md verification rows #22-#24:

  - 013 -> 012 clean downgrade (no dupes): uq_files_original_path restored,
    both agent_id columns relaxed back to nullable
  - 013 -> 012 fails LOUDLY when original_path collides across agents (D-16);
    error message contains 'Cannot downgrade 013->012' and the offending path;
    DB state is unchanged (DDL aborted pre-mutation)
  - 012 -> 011 clean downgrade: agents table dropped, agent_id columns gone,
    uq_files_original_path restored

These tests drive their own upgrade/downgrade sequence rather than using the
``migrated_engine`` fixture (which would always upgrade to head). Each test
cleans up in a ``finally:`` block via ``downgrade_to(cfg, 'base')``.

Operator pre-condition: the database ``phaze_migrations_test`` must exist on
``localhost:5432`` (see ``tests/test_migrations/conftest.py``).
"""

import asyncio
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


@pytest.mark.asyncio
async def test_downgrade_013_clean() -> None:
    """DATA-04: clean 013->012 downgrade restores uq_files_original_path and relaxes NOT NULL."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "013")
    await asyncio.to_thread(downgrade_to, cfg, "012")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            index_names = {r.indexname for r in (await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'files'"))).all()}
            files_agent_id_nullable = (
                await conn.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'files' AND column_name = 'agent_id'"))
            ).scalar_one()
            sb_agent_id_nullable = (
                await conn.execute(
                    text("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'scan_batches' AND column_name = 'agent_id'")
                )
            ).scalar_one()
        assert "uq_files_original_path" in index_names
        assert "uq_files_agent_id_original_path" not in index_names
        assert files_agent_id_nullable == "YES"
        assert sb_agent_id_nullable == "YES"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")


@pytest.mark.asyncio
async def test_downgrade_013_fails_on_dupes() -> None:
    """D-16: 013->012 raises RuntimeError when original_path collides across agents (T-V5-03 mitigation)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "013")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            # Insert a second agent and a duplicate original_path under both.
            await conn.execute(
                text(
                    "INSERT INTO agents (id, name, scan_roots, created_at, updated_at) "
                    "VALUES ('agent-b', 'agent-b', CAST('[]' AS jsonb), NOW(), NOW())"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                    "file_type, file_size, state, created_at, updated_at) "
                    "VALUES (:id, 'legacy-application-server', :hash, '/music/shared.mp3', 'shared.mp3', "
                    "'/music/shared.mp3', 'mp3', 1000, 'discovered', NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "hash": "a" * 64},
            )
            await conn.execute(
                text(
                    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                    "file_type, file_size, state, created_at, updated_at) "
                    "VALUES (:id, 'agent-b', :hash, '/music/shared.mp3', 'shared.mp3', "
                    "'/music/shared.mp3', 'mp3', 2000, 'discovered', NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "hash": "b" * 64},
            )

        # The downgrade MUST raise before mutating any DDL.
        with pytest.raises(RuntimeError, match="Cannot downgrade 013->012"):
            await asyncio.to_thread(downgrade_to, cfg, "012")

        # Confirm the downgrade aborted before swapping the unique index.
        async with engine.connect() as conn:
            index_names = {r.indexname for r in (await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'files'"))).all()}
        assert "uq_files_agent_id_original_path" in index_names

        # Clean up the duplicates so the final downgrade_to('base') in the finally block succeeds.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM files WHERE original_path = '/music/shared.mp3'"))
            await conn.execute(text("DELETE FROM agents WHERE id = 'agent-b'"))
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")


@pytest.mark.asyncio
async def test_downgrade_012_clean() -> None:
    """DATA-04: clean 012->011 drops agents table, removes agent_id columns, restores uq_files_original_path."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "012")
    await asyncio.to_thread(downgrade_to, cfg, "011")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            agents_exists = (await conn.execute(text("SELECT to_regclass('agents') AS t"))).scalar_one()
            files_cols = {
                r.column_name
                for r in (await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'files'"))).all()
            }
            sb_cols = {
                r.column_name
                for r in (await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'scan_batches'"))).all()
            }
            index_names = {r.indexname for r in (await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'files'"))).all()}
        assert agents_exists is None  # table dropped
        assert "agent_id" not in files_cols
        assert "agent_id" not in sb_cols
        assert "uq_files_original_path" in index_names
        assert "uq_files_agent_id_original_path" not in index_names
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
