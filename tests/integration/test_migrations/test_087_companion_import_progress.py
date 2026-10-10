"""Durable progress and physical acquisition evidence survive upgrade and refuse lossy rollback."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_empty_roundtrip_and_populated_progress_refuses_downgrade():
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await asyncio.to_thread(upgrade_to, cfg, "087")
        await asyncio.to_thread(downgrade_to, cfg, "086")
        await asyncio.to_thread(upgrade_to, cfg, "087")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO companion_import_runs(id,agent_id,continuation,associated,enumerated) VALUES(:id,'synthetic-agent',:step,false,false)"
                ),
                {"id": uuid.uuid4(), "step": uuid.uuid4()},
            )
        with pytest.raises(RuntimeError, match="lossy downgrade refused"):
            await asyncio.to_thread(downgrade_to, cfg, "086")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM companion_import_runs")) == 1
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "087"
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


async def test_acquisition_database_immutability_and_history_preservation():
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "086")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    source, observation, attempt = [uuid.uuid4() for _ in range(3)]
    try:
        async with engine.begin() as db:
            await db.execute(
                text("INSERT INTO provider_source_objects(id,provider_id,native_id,native_digest) VALUES(:id,'local','synthetic',:digest)"),
                {"id": source, "digest": "a" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO provider_source_observations(id,object_id,status,code,revision_scope,parser_version,content_digest,payload,truncated,retrieved_at) VALUES(:id,:source,'found','snapshot','unknown','test',:digest,'{}',false,now())"
                ),
                {"id": observation, "source": source, "digest": "b" * 64},
            )
        await asyncio.to_thread(upgrade_to, cfg, "087")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO provider_acquisition_attempts(attempt_id,source_object_id,observation_id,ordinal,agent_id,attempted_at,status,code,revision_scope,truncated,freshness,origin,envelope) VALUES(:id,:source,:observation,1,'synthetic-agent',now(),'found','snapshot','unknown',false,'current','capture','{}')"
                ),
                {"id": attempt, "source": source, "observation": observation},
            )
        for statement in ["UPDATE provider_acquisition_attempts SET code='changed'", "DELETE FROM provider_acquisition_attempts"]:
            with pytest.raises(DBAPIError, match="immutable"):
                async with engine.begin() as db:
                    await db.execute(text(statement))
        with pytest.raises(RuntimeError, match="lossy downgrade refused"):
            await asyncio.to_thread(downgrade_to, cfg, "086")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT observation_id FROM provider_acquisition_attempts")) == observation
            assert await db.scalar(text("SELECT id FROM provider_source_observations")) == observation
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
