"""Migration 090 admits a remote source object, leaves local rows untouched, and refuses a lossy rollback."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


INSERT = (
    "INSERT INTO provider_source_objects(id, provider_id, native_id, native_digest, source_file_id, original_file_id, channel) "
    "VALUES (:id, :provider, :native, :digest, :file, :file, :channel)"
)
LOCAL_ROWS = (
    ("local", "companion:synthetic-1", "companion"),
    ("local", "embedded:synthetic-2:comment", "embedded"),
    ("legacy_synthetic", "manual:synthetic-3", None),
)


async def _rows(engine: AsyncEngine) -> list[tuple[object, ...]]:
    async with engine.connect() as db:
        result = await db.execute(
            text("SELECT id, provider_id, native_id, native_digest, source_file_id, channel FROM provider_source_objects ORDER BY id")
        )
        return [tuple(row) for row in result]


async def _check(engine: AsyncEngine) -> str:
    async with engine.connect() as db:
        return str(
            await db.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_provider_source_objects_provider_channel'"))
        )


async def test_remote_channel_roundtrip_keeps_local_rows_and_refuses_lossy_downgrade() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await asyncio.to_thread(upgrade_to, cfg, "089")
        async with engine.begin() as db:
            await db.execute(text("INSERT INTO agents(id,name,kind,scan_roots) VALUES('migration-agent','migration-agent','fileserver','[]')"))
            file_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO files(id,agent_id,sha256_hash,original_path,current_path,original_filename,file_type,file_size) "
                    "VALUES(:id,'migration-agent',:sha,'/synthetic/set.mp3','/synthetic/set.mp3','/synthetic/set.mp3','mp3',0)"
                ),
                {"id": file_id, "sha": "a" * 64},
            )
            for provider, native, channel in LOCAL_ROWS:
                await db.execute(
                    text(INSERT),
                    {
                        "id": uuid.uuid4(),
                        "provider": provider,
                        "native": native,
                        "digest": "d" * 64,
                        "file": file_id if channel else None,
                        "channel": channel,
                    },
                )
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        text(INSERT),
                        {
                            "id": uuid.uuid4(),
                            "provider": "example_catalog",
                            "native": "set-01",
                            "digest": "e" * 64,
                            "file": None,
                            "channel": "remote",
                        },
                    )
        before = await _rows(engine)
        assert "'remote'" not in await _check(engine)

        await asyncio.to_thread(upgrade_to, cfg, "090")
        assert await _rows(engine) == before, "upgrade must leave every existing source object byte-identical"
        assert "'remote'" in await _check(engine)
        await asyncio.to_thread(downgrade_to, cfg, "089")
        assert await _rows(engine) == before
        assert "'remote'" not in await _check(engine)
        await asyncio.to_thread(upgrade_to, cfg, "090")
        assert await _rows(engine) == before

        async with engine.begin() as db:
            await db.execute(
                text(INSERT),
                {"id": uuid.uuid4(), "provider": "example_catalog", "native": "set-01", "digest": "e" * 64, "file": None, "channel": "remote"},
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as db:
                await db.execute(
                    text(INSERT),
                    {"id": uuid.uuid4(), "provider": "example_catalog", "native": "set-02", "digest": "f" * 64, "file": None, "channel": "web"},
                )
        with pytest.raises(RuntimeError, match="lossy downgrade refused"):
            await asyncio.to_thread(downgrade_to, cfg, "089")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM provider_source_objects WHERE channel = 'remote'")) == 1
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "090"
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
