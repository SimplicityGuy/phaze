"""The remote transport ledger applies and rolls back cleanly, and refuses to roll back spent budget."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_empty_roundtrip_constraints_and_populated_ledger_refuses_downgrade() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await asyncio.to_thread(upgrade_to, cfg, "089")
        await asyncio.to_thread(downgrade_to, cfg, "088")
        async with engine.connect() as db:
            tables = set((await db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars())
            assert not {"remote_transport_ledgers", "remote_transport_attempts"} & tables
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "088"
        await asyncio.to_thread(upgrade_to, cfg, "089")
        async with engine.begin() as db:
            await db.execute(text("INSERT INTO remote_transport_ledgers(provider_id, ordinal, last_reserved_at) VALUES ('synthetic', 1, now())"))
            await db.execute(
                text(
                    "INSERT INTO remote_transport_attempts(provider_id, ordinal, reserved_at, request_kind, run_id) VALUES ('synthetic', 1, now(), 'fetch', :run)"
                ),
                {"run": uuid.uuid4()},
            )
        for statement in [
            "UPDATE remote_transport_ledgers SET ordinal = -1",
            "UPDATE remote_transport_ledgers SET last_reserved_at = NULL",
            "UPDATE remote_transport_ledgers SET stop_code = 'http_403'",
            "UPDATE remote_transport_attempts SET request_kind = 'post'",
            "DELETE FROM remote_transport_ledgers",
        ]:
            with pytest.raises(IntegrityError):
                async with engine.begin() as db:
                    await db.execute(text(statement))
        with pytest.raises(RuntimeError, match="lossy downgrade refused"):
            await asyncio.to_thread(downgrade_to, cfg, "088")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM remote_transport_attempts")) == 1
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "089"
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
