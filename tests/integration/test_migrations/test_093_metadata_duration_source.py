"""Migration 093 adds the nullable ``metadata.duration_source`` column reversibly (phaze-v03x9)."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_CHECK = "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_metadata_duration_source'"
_COLUMN = (
    "SELECT data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name = 'metadata' AND column_name = 'duration_source'"
)


async def test_the_duration_source_is_a_nullable_text_with_no_default_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "092")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text(_COLUMN))).first() is None
        await asyncio.to_thread(upgrade_to, cfg, "093")
        async with engine.connect() as connection:
            assert tuple((await connection.execute(text(_COLUMN))).one()) == ("text", "YES", None)
            definition = (await connection.execute(text(_CHECK))).scalar_one()
            assert "tags" in definition
            assert "audio_frames" in definition
        await asyncio.to_thread(downgrade_to, cfg, "092")
        async with engine.connect() as connection:
            assert (await connection.execute(text(_COLUMN))).first() is None
            assert (await connection.execute(text(_CHECK))).first() is None
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
