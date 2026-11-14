"""Migration 099 adds ``analysis_embedding`` reversibly and additively (phaze-g79ea).

The ORM half is ``tests/shared/models/test_analysis_embedding.py``; the restricted non-superuser path is
``test_restricted_app_role.py`` (the whole chain, 099 included, runs as the app role).
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_AGENT = "INSERT INTO agents (id, name, token_hash, scan_roots) VALUES ('itest-agent', 'itest-agent', repeat('a', 64), '[\"/archive\"]')"
_FILE = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, file_type, file_size) "
    "VALUES ('00000000-0000-0000-0000-000000000001', 'itest-agent', repeat('b', 64), '/archive/<set-01>.mp3', '<set-01>.mp3', '/archive/<set-01>.mp3', 'mp3', 10)"
)
_INSERT = (
    "INSERT INTO analysis_embedding (file_id, model, window_index, dim, vector) "
    "VALUES ('00000000-0000-0000-0000-000000000001', :model, :window_index, :dim, :vector)"
)
_VECTOR = bytes(range(256)) * 10  # 2,560 B = 1280 float16


async def _tables(connection: object) -> set[str]:
    rows = await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))  # type: ignore[attr-defined]
    return set(rows.scalars().all())


async def _refused(engine: object, params: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:  # type: ignore[attr-defined]
            await connection.execute(text(_INSERT), params)


async def test_099_creates_the_table_enforces_its_shape_cascades_with_files_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "098")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            assert "analysis_embedding" not in await _tables(connection)
            await connection.execute(text(_AGENT))
            await connection.execute(text(_FILE))

        await asyncio.to_thread(upgrade_to, cfg, "099")
        good = {"model": "m", "window_index": 0, "dim": 1280, "vector": _VECTOR}
        async with engine.begin() as connection:
            await connection.execute(text(_INSERT), good)
            stored = (await connection.execute(text("SELECT model, window_index, dim, vector FROM analysis_embedding"))).one()
            assert (stored[0], stored[1], stored[2], bytes(stored[3])) == ("m", 0, 1280, _VECTOR)

        await _refused(engine, good)  # the key is (file_id, model, window_index)
        await _refused(engine, {**good, "window_index": 1, "vector": _VECTOR[:-2]})  # not dim x 2 bytes
        await _refused(engine, {**good, "window_index": -1})
        await _refused(engine, {**good, "window_index": 2, "dim": 0, "vector": b""})
        async with engine.begin() as connection:
            await connection.execute(text(_INSERT), {**good, "model": "other"})  # another model, same window: fine
            await connection.execute(text("DELETE FROM files"))
            assert (await connection.execute(text("SELECT count(*) FROM analysis_embedding"))).scalar_one() == 0, "ON DELETE CASCADE did not fire"

        await asyncio.to_thread(downgrade_to, cfg, "098")
        async with engine.connect() as connection:
            assert "analysis_embedding" not in await _tables(connection)
        await asyncio.to_thread(upgrade_to, cfg, "099")
        async with engine.connect() as connection:
            assert "analysis_embedding" in await _tables(connection)
    finally:
        await engine.dispose()
