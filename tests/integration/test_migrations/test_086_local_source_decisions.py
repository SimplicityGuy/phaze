"""Nullable expansion preserves unknown history and refuses every form of review evidence loss."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_expansion_unknown_history_and_each_lossy_column_guard():
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "085")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    media, companion, link, source, observation, event = [uuid.uuid4() for _ in range(6)]
    try:
        async with engine.begin() as db:
            await db.execute(text("INSERT INTO agents(id,name,kind,scan_roots) VALUES('migration-agent','migration-agent','fileserver','[]')"))
            for identifier, name, kind in ((media, "media.mp3", "mp3"), (companion, "notes.txt", "txt")):
                await db.execute(
                    text(
                        "INSERT INTO files(id,agent_id,sha256_hash,original_path,current_path,original_filename,file_type,file_size) VALUES(:id,'migration-agent',:sha,:name,:name,:name,:kind,0)"
                    ),
                    {"id": identifier, "sha": "a" * 64, "name": "/synthetic/" + name, "kind": kind},
                )
            await db.execute(
                text("INSERT INTO file_companions(id,companion_id,media_id) VALUES(:id,:companion,:media)"),
                {"id": link, "companion": companion, "media": media},
            )
            await db.execute(
                text("INSERT INTO provider_source_objects(id,provider_id,native_id,native_digest) VALUES(:id,'local','synthetic',:digest)"),
                {"id": source, "digest": "b" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO provider_source_observations(id,object_id,status,code,revision_scope,parser_version,content_digest,payload,truncated,retrieved_at) VALUES(:id,:source,'found','snapshot','unknown','test',:digest,'{}',false,now())"
                ),
                {"id": observation, "source": source, "digest": "c" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO provider_recording_selections(media_id,kind,observation_id,actor) VALUES(:media,'tracklist',:observation,'old-review')"
                ),
                {"media": media, "observation": observation},
            )
            await db.execute(
                text(
                    "INSERT INTO provider_selection_events(id,media_id,kind,observation_id,actor) VALUES(:id,:media,'tracklist',:observation,'old-review')"
                ),
                {"id": event, "media": media, "observation": observation},
            )
        await asyncio.to_thread(upgrade_to, cfg, "086")
        async with engine.connect() as db:
            assert tuple(
                (await db.execute(text("SELECT derivation_method,derivation_revision,derivation_evidence,derived_at FROM file_companions"))).one()
            ) == (None, None, None, None)
            assert tuple((await db.execute(text("SELECT selection_token,target_mapping FROM provider_recording_selections"))).one()) == (None, None)
        # Empty expansion can round-trip without touching old accepted choices/links.
        await asyncio.to_thread(downgrade_to, cfg, "085")
        await asyncio.to_thread(upgrade_to, cfg, "086")
        for column, sql_value in [
            ("derivation_method", "'reference'"),
            ("derivation_revision", "'a'"),
            ("derivation_evidence", "'[]'::jsonb"),
            ("derived_at", "now()"),
        ]:
            async with engine.begin() as db:
                await db.execute(text(f"UPDATE file_companions SET {column}={sql_value}"))  # noqa: S608 -- fixed synthetic column/value list
            with pytest.raises(RuntimeError, match="lossy downgrade refused"):
                await asyncio.to_thread(downgrade_to, cfg, "085")
            async with engine.begin() as db:
                await db.execute(text(f"UPDATE file_companions SET {column}=NULL"))  # noqa: S608 -- fixed synthetic column list
        for column, sql_value in [("selection_token", f"'{uuid.uuid4()}'::uuid"), ("target_mapping", "'{}'::jsonb")]:
            async with engine.begin() as db:
                await db.execute(text(f"UPDATE provider_recording_selections SET {column}={sql_value}"))  # noqa: S608 -- fixed synthetic column/value list
            with pytest.raises(RuntimeError, match="lossy downgrade refused"):
                await asyncio.to_thread(downgrade_to, cfg, "085")
            async with engine.begin() as db:
                await db.execute(text(f"UPDATE provider_recording_selections SET {column}=NULL"))  # noqa: S608 -- fixed synthetic column list
        for statement in ["UPDATE provider_selection_events SET actor='changed'", "DELETE FROM provider_selection_events"]:
            with pytest.raises(DBAPIError, match="immutable"):
                async with engine.begin() as db:
                    await db.execute(text(statement))
        # Each nullable event field independently makes downgrade lossy, even without decision ID.
        for column, value in [
            ("action", "'reject'"),
            ("target_mapping", "'{}'::jsonb"),
            ("decision_payload", "'{}'::jsonb"),
            ("decision_id", f"'{uuid.uuid4()}'::uuid"),
        ]:
            async with engine.begin() as db:
                await db.execute(
                    text(
                        f"INSERT INTO provider_selection_events(id,media_id,kind,observation_id,actor,{column}) VALUES(:id,:media,'release_metadata',:observation,'review',{value})"
                    ),
                    {"id": uuid.uuid4(), "media": media, "observation": observation},
                )
            with pytest.raises(RuntimeError, match="lossy downgrade refused"):
                await asyncio.to_thread(downgrade_to, cfg, "085")
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
