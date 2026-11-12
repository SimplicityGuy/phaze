"""Migration 097 builds the files FIFO index the cloud staging walk pages on, reversibly (phaze-2al7q.3)."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_INDEX = "SELECT indisvalid, pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass('public.ix_files_created_at_id')"


async def test_the_fifo_index_is_built_valid_self_heals_an_invalid_leftover_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "096")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text(_INDEX))).first() is None
        await asyncio.to_thread(upgrade_to, cfg, "097")
        async with engine.connect() as connection:
            valid, definition = (await connection.execute(text(_INDEX))).one()
        assert valid
        assert definition.endswith("USING btree (created_at, id)"), definition
        await asyncio.to_thread(downgrade_to, cfg, "096")
        async with engine.connect() as connection:
            assert (await connection.execute(text(_INDEX))).first() is None
        # An interrupted CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would skip forever.
        async with engine.begin() as connection:
            await connection.execute(text("CREATE INDEX ix_files_created_at_id ON public.files USING btree (created_at, id)"))
            await connection.execute(text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass('public.ix_files_created_at_id')"))
        await asyncio.to_thread(upgrade_to, cfg, "097")
        async with engine.connect() as connection:
            assert (await connection.execute(text(_INDEX))).one()[0], "the invalid leftover was dropped and rebuilt"
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
