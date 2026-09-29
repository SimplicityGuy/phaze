"""Migration 071 (phaze-j0ixx): the ``backend_breaker`` table.

* ``test_upgrade_creates_an_empty_breaker_table_matching_the_model`` -- 070 -> 071 creates the table
  with the model's columns, every one nullable except the key and the timestamps, and no rows (every
  backend starts closed); an existing ``cloud_job`` row is untouched.
* ``test_downgrade_drops_the_table_and_upgrade_restores_it`` -- 071 -> 070 removes exactly the table
  and a second upgrade succeeds (a clean round trip).
"""

import asyncio
from collections.abc import AsyncGenerator
import uuid

from alembic.config import Config
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_PREVIOUS_REVISION = "070"
_THIS_REVISION = "071"
_EXPECTED = {
    "backend_id": ("character varying", "NO"),
    "tripped_at": ("timestamp with time zone", "YES"),
    "trip_reason": ("text", "YES"),
    "next_probe_at": ("timestamp with time zone", "YES"),
    "reset_at": ("timestamp with time zone", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}


@pytest_asyncio.fixture
async def engine_at_070_with_a_row() -> AsyncGenerator[tuple[AsyncEngine, Config, uuid.UUID]]:
    """Reset the migrations DB, upgrade to 070, and seed one in-flight ``cloud_job`` row."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, _PREVIOUS_REVISION)
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    file_id = uuid.uuid4()
    agent_id = f"breaker-agent-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"), {"id": agent_id}
        )
        await conn.execute(
            text(
                "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                "VALUES (:id, :sha, :path, 'song.mp3', :path, 'mp3', 1024, :agent_id)"
            ),
            {"id": file_id, "sha": uuid.uuid4().hex * 2, "path": f"/test/music/{file_id}/song.mp3", "agent_id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO cloud_job (id, file_id, status, kueue_workload, attempts, backend_id, last_exit_code) "
                "VALUES (:id, :file_id, 'submitted', 'phaze-analyze-x', 1, 'burst', 14)"
            ),
            {"id": uuid.uuid4(), "file_id": file_id},
        )
    try:
        yield engine, cfg, file_id
    finally:
        await engine.dispose()


async def _columns(engine: AsyncEngine) -> dict[str, tuple[str, str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'backend_breaker'"
            )
        )
    return {name: (data_type, nullable) for name, data_type, nullable in rows.all()}


@pytest.mark.asyncio
async def test_upgrade_creates_an_empty_breaker_table_matching_the_model(engine_at_070_with_a_row: tuple[AsyncEngine, Config, uuid.UUID]) -> None:
    engine, cfg, file_id = engine_at_070_with_a_row
    assert await _columns(engine) == {}

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    assert await _columns(engine) == _EXPECTED
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM backend_breaker"))).scalar_one() == 0
        row = (await conn.execute(text("SELECT status, attempts, last_exit_code FROM cloud_job WHERE file_id = :f"), {"f": file_id})).one()
    assert tuple(row) == ("submitted", 1, 14)


@pytest.mark.asyncio
async def test_downgrade_drops_the_table_and_upgrade_restores_it(engine_at_070_with_a_row: tuple[AsyncEngine, Config, uuid.UUID]) -> None:
    engine, cfg, file_id = engine_at_070_with_a_row
    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    await asyncio.to_thread(downgrade_to, cfg, _PREVIOUS_REVISION)

    assert await _columns(engine) == {}
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE file_id = :f"), {"f": file_id})).scalar_one() == 1

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    assert await _columns(engine) == _EXPECTED
