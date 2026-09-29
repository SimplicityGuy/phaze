"""Migration 070 (phaze-o71bf): the ``tracklist_file_lookups`` per-file record.

* ``test_upgrade_creates_the_table_keyed_and_cascaded_on_files`` -- previous -> 070 creates the table
  with the ORM's columns and nullability, ``file_id`` as the primary key, and a ``files.id`` foreign
  key that cascades: deleting a file deletes its record.
* ``test_downgrade_drops_the_table_and_upgrade_round_trips`` -- 070 -> previous removes the table and
  nothing else, and upgrading again recreates it (the round trip a rollback-then-redeploy performs).

The previous revision is read from the script directory rather than spelled here: 070 is re-pointed
onto whichever revision is head on ``main`` when it lands, and the test must follow it.
"""

import asyncio
from collections.abc import AsyncGenerator
import uuid

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    _reset_schema,
    downgrade_to,
    upgrade_to,
)


_THIS_REVISION = "070"
_TABLE = "tracklist_file_lookups"
_EXPECTED_COLUMNS = {
    "file_id": "NO",
    "outcome": "NO",
    "set_key": "YES",
    "last_attempt_at": "YES",
    "next_eligible_at": "YES",
    "created_at": "NO",
    "updated_at": "NO",
}


def _previous_revision(cfg: Config) -> str:
    down = ScriptDirectory.from_config(cfg).get_revision(_THIS_REVISION).down_revision
    assert isinstance(down, str), "070 must have exactly one parent"
    return down


@pytest_asyncio.fixture
async def engine_at_previous_revision() -> AsyncGenerator[tuple[AsyncEngine, Config]]:
    """Reset the migrations DB, upgrade it to 070's parent, and yield an engine plus the alembic config."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, _previous_revision(cfg))
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        yield engine, cfg
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


async def _columns(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"),
            {"t": _TABLE},
        )
    return dict(rows.all())  # type: ignore[arg-type]


async def _seed_file(engine: AsyncEngine) -> uuid.UUID:
    agent_id, file_id = f"tl-lookup-agent-{uuid.uuid4().hex[:8]}", uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                "VALUES (:id, :sha, :path, 'set.mp3', :path, 'mp3', 1024, :agent_id)"
            ),
            {"id": file_id, "sha": uuid.uuid4().hex * 2, "path": f"/test/music/{file_id}/set.mp3", "agent_id": agent_id},
        )
    return file_id


@pytest.mark.asyncio
async def test_upgrade_creates_the_table_keyed_and_cascaded_on_files(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    engine, cfg = engine_at_previous_revision
    assert await _columns(engine) == {}

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    assert await _columns(engine) == _EXPECTED_COLUMNS
    file_id = await _seed_file(engine)
    async with engine.begin() as conn:
        await conn.execute(text(f"INSERT INTO {_TABLE} (file_id, outcome) VALUES (:id, 'queued')"), {"id": file_id})  # noqa: S608 - constant table name
        duplicate = conn.begin_nested()
        await duplicate.start()
        with pytest.raises(Exception, match="pk_tracklist_file_lookups"):
            await conn.execute(text(f"INSERT INTO {_TABLE} (file_id, outcome) VALUES (:id, 'matched')"), {"id": file_id})  # noqa: S608
        await duplicate.rollback()
        await conn.execute(text("DELETE FROM files WHERE id = :id"), {"id": file_id})
        remaining = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE} WHERE file_id = :id"), {"id": file_id})).scalar_one()  # noqa: S608
    assert remaining == 0, "deleting a file must cascade to its lookup record"


@pytest.mark.asyncio
async def test_downgrade_drops_the_table_and_upgrade_round_trips(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    engine, cfg = engine_at_previous_revision
    previous = _previous_revision(cfg)

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    await asyncio.to_thread(downgrade_to, cfg, previous)

    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == previous
    assert await _columns(engine) == {}

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    assert await _columns(engine) == _EXPECTED_COLUMNS
