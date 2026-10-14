"""Migration 091 adds the per-object read sequence and backfills it in (retrieved_at, id) order (phaze-z9ea3)."""

import asyncio
from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_COLUMNS = "SELECT table_name FROM information_schema.columns WHERE column_name = 'read_seq' ORDER BY table_name"


async def test_the_sequence_is_backfilled_by_retrieval_order_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "090")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    obj, late, early = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as db:
            await db.execute(
                text("INSERT INTO provider_source_objects (id,provider_id,native_id,native_digest,channel) VALUES (:id,'p','n',:d,'remote')"),
                {"id": obj, "d": "a" * 64},
            )
            for oid, at in ((late, datetime(2026, 1, 2, tzinfo=UTC)), (early, datetime(2026, 1, 1, tzinfo=UTC))):
                await db.execute(
                    text(
                        "INSERT INTO provider_source_observations (id,object_id,status,code,revision_scope,parser_version,content_digest,payload,truncated,retrieved_at) "
                        "VALUES (:id,:o,'found','c','unknown','v',:d,'{}',false,:at)"
                    ),
                    {"id": oid, "o": obj, "d": str(oid.int)[:8], "at": at},
                )
        await asyncio.to_thread(upgrade_to, cfg, "091")
        async with engine.connect() as db:
            assert [r[0] for r in (await db.execute(text(_COLUMNS))).all()] == ["provider_source_objects", "provider_source_reads"]
            seqs = dict((await db.execute(text("SELECT observation_id, read_seq FROM provider_source_reads"))).all())
            assert (seqs[early], seqs[late]) == (1, 2)
            assert (await db.execute(text("SELECT read_seq FROM provider_source_objects"))).scalar_one() == 2
        # The real migrated schema, trigger included: observations stay immutable, while a new read still appends.
        async with engine.begin() as db:
            await db.execute(
                text("INSERT INTO provider_source_reads (object_id, read_seq, observation_id) VALUES (:o, 3, :ob)"), {"o": obj, "ob": early}
            )
        async with engine.connect() as db:
            with pytest.raises(DBAPIError, match="immutable"):
                await db.execute(text("UPDATE provider_source_observations SET code = 'x'"))
        await asyncio.to_thread(downgrade_to, cfg, "090")
        async with engine.connect() as db:
            assert (await db.execute(text(_COLUMNS))).all() == []
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
