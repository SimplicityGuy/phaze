"""Migration 084 adds the nullable ``files.companion_ambiguous_at`` marker reversibly (phaze-st1ty)."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_COLUMN = "SELECT data_type, is_nullable, column_default FROM information_schema.columns WHERE table_name = 'files' AND column_name = 'companion_ambiguous_at'"


async def test_the_ambiguity_marker_is_a_nullable_timestamptz_with_no_default_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "083")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text(_COLUMN))).first() is None
        await asyncio.to_thread(upgrade_to, cfg, "084")
        async with engine.connect() as connection:
            assert tuple((await connection.execute(text(_COLUMN))).one()) == ("timestamp with time zone", "YES", None)
        await asyncio.to_thread(downgrade_to, cfg, "083")
        async with engine.connect() as connection:
            assert (await connection.execute(text(_COLUMN))).first() is None
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
