"""Migration 069 (phaze-1xngw): ``cloud_job`` gains the reconcile cron's last-failure record.

* ``test_upgrade_adds_three_nullable_columns_and_leaves_existing_rows_null`` -- 068 -> 069 adds
  ``last_exit_code`` / ``last_failure_reason`` / ``last_failed_at`` as NULLABLE, and a row written
  before the upgrade reads NULL in all three with its other columns intact.
* ``test_downgrade_drops_the_columns_and_upgrade_restores_them`` -- 069 -> 068 removes exactly the
  three columns, keeps the row, and a second upgrade to 069 succeeds (a clean round trip).
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


_PREVIOUS_REVISION = "068"
_THIS_REVISION = "069"
_NEW_COLUMNS = {"last_exit_code": "integer", "last_failure_reason": "text", "last_failed_at": "timestamp with time zone"}


@pytest_asyncio.fixture
async def engine_at_068_with_a_row() -> AsyncGenerator[tuple[AsyncEngine, Config, uuid.UUID]]:
    """Reset the migrations DB, upgrade to 068, and seed one in-flight ``cloud_job`` row."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, _PREVIOUS_REVISION)
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    file_id = uuid.uuid4()
    agent_id = f"failure-agent-{uuid.uuid4().hex[:8]}"
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
            text("INSERT INTO cloud_job (id, file_id, status, kueue_workload, attempts) VALUES (:id, :file_id, 'submitted', 'phaze-analyze-x', 2)"),
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
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'cloud_job'"
            )
        )
    return {name: (data_type, nullable) for name, data_type, nullable in rows.all()}


@pytest.mark.asyncio
async def test_upgrade_adds_three_nullable_columns_and_leaves_existing_rows_null(
    engine_at_068_with_a_row: tuple[AsyncEngine, Config, uuid.UUID],
) -> None:
    engine, cfg, file_id = engine_at_068_with_a_row
    assert set(_NEW_COLUMNS).isdisjoint(await _columns(engine))

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    columns = await _columns(engine)
    for name, data_type in _NEW_COLUMNS.items():
        assert columns[name] == (data_type, "YES")
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, attempts, last_exit_code, last_failure_reason, last_failed_at FROM cloud_job WHERE file_id = :f"), {"f": file_id}
            )
        ).one()
    assert tuple(row) == ("submitted", 2, None, None, None)


@pytest.mark.asyncio
async def test_downgrade_drops_the_columns_and_upgrade_restores_them(engine_at_068_with_a_row: tuple[AsyncEngine, Config, uuid.UUID]) -> None:
    engine, cfg, file_id = engine_at_068_with_a_row
    before = await _columns(engine)
    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    await asyncio.to_thread(downgrade_to, cfg, _PREVIOUS_REVISION)

    assert await _columns(engine) == before
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE file_id = :f"), {"f": file_id})).scalar_one() == 1

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    assert set(_NEW_COLUMNS) <= set(await _columns(engine))
