"""Migration 077 preserves existing obligations and upgrades/downgrades reversibly."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_terminal_outcome_migration_preserves_existing_ledger() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "076")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO scheduling_ledger (key, function, routing, payload) VALUES ('process_file:synthetic', 'process_file', 'agent', '{}')"
                )
            )
            before = (await connection.execute(text("SELECT key, enqueued_at, payload FROM scheduling_ledger"))).one()
        await asyncio.to_thread(upgrade_to, cfg, "077")
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT key, enqueued_at, payload, terminal_at FROM scheduling_ledger"))).one()
            assert tuple(row[:3]) == tuple(before) and row.terminal_at is None
            column = (
                await connection.execute(
                    text(
                        "SELECT data_type, is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'scheduling_ledger' AND column_name = 'terminal_at'"
                    )
                )
            ).one()
            assert tuple(column) == ("timestamp with time zone", "YES")
        await asyncio.to_thread(downgrade_to, cfg, "076")
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT key, enqueued_at, payload FROM scheduling_ledger"))).one() == before
        await asyncio.to_thread(upgrade_to, cfg, "077")
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT terminal_at FROM scheduling_ledger"))).scalar_one() is None
    finally:
        await engine.dispose()
