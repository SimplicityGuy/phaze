"""Migration 068 (phaze-3agnm) gives stat-less filter columns planner statistics.

The fixture writes fewer rows than autoanalyze's base threshold (50), so no ``pg_stats`` row can
appear before the upgrade by any route but the migration itself -- the same "table written, never
analyzed" state host-prod's ``metadata`` was in.
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


_ROWS = 5
_STATS_SQL = text(
    "SELECT tablename, attname, null_frac FROM pg_stats WHERE schemaname = 'public' AND (tablename, attname) IN (('metadata', 'failed_at'), ('cloud_job', 'telemetry_slot'))"
)


@pytest_asyncio.fixture
async def engine_at_067() -> AsyncGenerator[tuple[AsyncEngine, Config]]:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "067")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        yield engine, cfg
    finally:
        await engine.dispose()


async def _stats(engine: AsyncEngine) -> dict[tuple[str, str], float]:
    async with engine.connect() as conn:
        return {(table, column): null_frac for table, column, null_frac in (await conn.execute(_STATS_SQL)).all()}


@pytest.mark.asyncio
async def test_upgrade_analyzes_metadata_and_cloud_job(engine_at_067: tuple[AsyncEngine, Config]) -> None:
    engine, cfg = engine_at_067
    agent_id = f"analyze-agent-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"), {"id": agent_id}
        )
        for _ in range(_ROWS):
            file_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                    "VALUES (:id, :sha, :path, 'song.mp3', :path, 'mp3', 1024, :agent_id)"
                ),
                {"id": file_id, "sha": uuid.uuid4().hex * 2, "path": f"/test/music/{file_id}/song.mp3", "agent_id": agent_id},
            )
            await conn.execute(text("INSERT INTO metadata (id, file_id) VALUES (:id, :file_id)"), {"id": uuid.uuid4(), "file_id": file_id})
            await conn.execute(
                text("INSERT INTO cloud_job (id, file_id, status) VALUES (:id, :file_id, 'succeeded')"), {"id": uuid.uuid4(), "file_id": file_id}
            )
    assert await _stats(engine) == {}

    await asyncio.to_thread(upgrade_to, cfg, "068")

    # Every row has a NULL in both columns, which is exactly the fact the planner lacked.
    assert await _stats(engine) == {("metadata", "failed_at"): 1.0, ("cloud_job", "telemetry_slot"): 1.0}


@pytest.mark.asyncio
async def test_upgrade_on_empty_tables_and_no_op_downgrade(engine_at_067: tuple[AsyncEngine, Config]) -> None:
    engine, cfg = engine_at_067
    await asyncio.to_thread(upgrade_to, cfg, "068")
    await asyncio.to_thread(downgrade_to, cfg, "067")
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "067"
