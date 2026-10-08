"""Migration 079 creates companion_content_features reversibly, cascading with its files row (phaze-osy6j)."""

import asyncio

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
_FEATURES = (
    "INSERT INTO companion_content_features (file_id, agent_id, fingerprint, encoding, byte_size, is_tracklist, junk_class, extractor_version) "
    "VALUES ('00000000-0000-0000-0000-000000000001', 'itest-agent', repeat('b', 64), :encoding, 10, false, :junk, 1)"
)


async def _tables(connection: object) -> set[str]:
    rows = await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))  # type: ignore[attr-defined]
    return set(rows.scalars().all())


async def test_the_table_is_created_cascades_with_files_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "078")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            assert "companion_content_features" not in await _tables(connection)
            await connection.execute(text(_AGENT))
            await connection.execute(text(_FILE))
        await asyncio.to_thread(upgrade_to, cfg, "079")
        async with engine.begin() as connection:
            await connection.execute(text(_FEATURES), {"encoding": "cp437", "junk": "known_stamp"})
            row = (
                await connection.execute(text("SELECT media_references, folder_media, reference_count, truncated FROM companion_content_features"))
            ).one()
            assert tuple(row) == ([], [], 0, False)
        # The CHECK constraints refuse what no writer may produce.
        for encoding, junk in (("cp1251", None), ("ascii", "deleted")):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(text("DELETE FROM companion_content_features"))
                    await connection.execute(text(_FEATURES), {"encoding": encoding, "junk": junk})
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM files"))
            assert (await connection.execute(text("SELECT count(*) FROM companion_content_features"))).scalar_one() == 0
        await asyncio.to_thread(downgrade_to, cfg, "078")
        async with engine.connect() as connection:
            assert "companion_content_features" not in await _tables(connection)
        await asyncio.to_thread(upgrade_to, cfg, "079")
        async with engine.connect() as connection:
            assert "companion_content_features" in await _tables(connection)
    finally:
        await engine.dispose()
