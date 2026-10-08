"""Migration 081 builds the files-by-agent-and-folder expression index reversibly (phaze-4x319.5)."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_INDEX = "SELECT indisvalid, pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass('public.ix_files_agent_id_folder')"


async def test_the_folder_index_is_built_valid_on_the_models_expression_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "080")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text(_INDEX))).first() is None
        await asyncio.to_thread(upgrade_to, cfg, "081")
        async with engine.connect() as connection:
            valid, definition = (await connection.execute(text(_INDEX))).one()
        assert valid
        assert "(agent_id, regexp_replace(original_path, '/[^/]*$'::text, ''::text))" in definition
        await asyncio.to_thread(downgrade_to, cfg, "080")
        async with engine.connect() as connection:
            assert (await connection.execute(text(_INDEX))).first() is None
        await asyncio.to_thread(upgrade_to, cfg, "081")
        async with engine.connect() as connection:
            assert (await connection.execute(text(_INDEX))).one()[0]
    finally:
        await engine.dispose()
