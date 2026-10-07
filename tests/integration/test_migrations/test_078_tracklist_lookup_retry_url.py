"""Migration 078 adds the nullable retry hint without touching existing cache rows, and downgrades cleanly."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_retry_url_migration_preserves_rows_and_downgrades_cleanly() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "077")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tracklist_lookup_cache (id, set_key, query_text, outcome, source_url, attempts) "
                    "VALUES (gen_random_uuid(), :key, 'synthetic query', 'blocked', 'https://www.1001tracklists.com/tracklist/abc/x.html', 2)"
                ),
                {"key": "k" * 64},
            )
            before = (await connection.execute(text("SELECT set_key, outcome, source_url, attempts FROM tracklist_lookup_cache"))).one()

        await asyncio.to_thread(upgrade_to, cfg, "078")
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT set_key, outcome, source_url, attempts, retry_url FROM tracklist_lookup_cache"))).one()
            assert tuple(row[:4]) == tuple(before) and row.retry_url is None, "existing rows are untouched and carry no hint"
            column = (
                await connection.execute(
                    text(
                        "SELECT data_type, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'tracklist_lookup_cache' AND column_name = 'retry_url'"
                    )
                )
            ).one()
            assert tuple(column) == ("text", "YES")

        await asyncio.to_thread(downgrade_to, cfg, "077")
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT set_key, outcome, source_url, attempts FROM tracklist_lookup_cache"))).one() == before
            gone = await connection.execute(
                text("SELECT 1 FROM information_schema.columns WHERE table_name = 'tracklist_lookup_cache' AND column_name = 'retry_url'")
            )
            assert gone.first() is None

        await asyncio.to_thread(upgrade_to, cfg, "078")
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT retry_url FROM tracklist_lookup_cache"))).scalar_one() is None
    finally:
        await engine.dispose()
