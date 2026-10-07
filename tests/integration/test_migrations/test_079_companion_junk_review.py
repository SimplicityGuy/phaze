"""Migration 079 creates companion_junk_review reversibly, FK-free, one live row per identity (phaze-bk5jp)."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_AGENT = "INSERT INTO agents (id, name, token_hash, scan_roots) VALUES ('itest-agent', 'itest-agent', repeat('a', 64), '[\"/archive\"]')"
_FILE = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, file_type, file_size) "
    "VALUES ('00000000-0000-0000-0000-000000000001', 'itest-agent', repeat('b', 64), '/archive/<set-01>.nfo', '<set-01>.nfo', "
    "'/archive/<set-01>.nfo', 'nfo', 10)"
)
_REVIEW = (
    "INSERT INTO companion_junk_review (id, agent_id, original_path, sha256_hash, file_id, file_type, file_size, reason, content_group, "
    "status, decided_at, executed_at) VALUES (gen_random_uuid(), 'itest-agent', '/archive/<set-01>.nfo', repeat('b', 64), "
    "'00000000-0000-0000-0000-000000000001', :file_type, 10, :reason, repeat('b', 64), :status, :decided_at, :executed_at)"
)
_AT = datetime(2026, 10, 7, tzinfo=UTC)
_OK = {"file_type": "nfo", "reason": "known_stamp", "status": "pending", "decided_at": None, "executed_at": None}


async def _tables(connection: object) -> set[str]:
    rows = await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))  # type: ignore[attr-defined]
    return set(rows.scalars().all())


async def test_the_table_is_created_fk_free_with_one_live_row_per_identity_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "078")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            assert "companion_junk_review" not in await _tables(connection)
            await connection.execute(text(_AGENT))
            await connection.execute(text(_FILE))
        await asyncio.to_thread(upgrade_to, cfg, "079")
        async with engine.begin() as connection:
            foreign_keys = await connection.execute(
                text("SELECT count(*) FROM pg_constraint WHERE conrelid = 'companion_junk_review'::regclass AND contype = 'f'")
            )
            assert foreign_keys.scalar_one() == 0
            # A quarantined (terminal) row and a live row may share the identity...
            await connection.execute(text(_REVIEW), {**_OK, "status": "quarantined", "decided_at": _AT, "executed_at": _AT})
            await connection.execute(text(_REVIEW), _OK)
        # ...but never two live rows, and the CHECKs refuse what no writer may produce.
        for overrides in (
            {},
            {"file_type": "mp3"},
            {"reason": "big"},
            {"status": "deleted"},
            {"status": "approved"},  # a decision with no decided_at
            {"status": "failed", "decided_at": _AT},  # terminal with no executed_at
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text("DELETE FROM companion_junk_review WHERE status <> 'quarantined'") if overrides else text("SELECT 1")
                    )
                    await connection.execute(text(_REVIEW), {**_OK, **overrides})
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM files"))
            assert (await connection.execute(text("SELECT count(*) FROM companion_junk_review"))).scalar_one() == 2
        await asyncio.to_thread(downgrade_to, cfg, "078")
        async with engine.connect() as connection:
            assert "companion_junk_review" not in await _tables(connection)
        await asyncio.to_thread(upgrade_to, cfg, "079")
        async with engine.connect() as connection:
            assert "companion_junk_review" in await _tables(connection)
    finally:
        await engine.dispose()
