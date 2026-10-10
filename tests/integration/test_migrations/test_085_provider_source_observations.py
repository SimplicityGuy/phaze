"""Seeded expansion preserves reviewed legacy data and refuses evidence loss."""

import asyncio
from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from phaze.models.provider_source import ProviderSourceObservation
from phaze.services.provider_persistence import backfill_legacy_identities, store_snapshot
from phaze.tracklist_providers.domain import Completeness, ProviderTrack, Snapshot, SourceIdentity

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_seeded_upgrade_backfill_restart_and_lossy_downgrade_refusal() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "084")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    tid, projection, vid, track, link = (uuid.uuid4() for _ in range(5))
    try:
        async with engine.begin() as db:
            for identifier, propagation in ((tid, None), (projection, "k" * 64)):
                await db.execute(
                    text(
                        "INSERT INTO tracklists (id,external_id,source_url,source,status,auto_linked,propagated_from_set_key) VALUES (:id,'seed','', 'historical-provider','approved',false,:prop)"
                    ),
                    {"id": identifier, "prop": propagation},
                )
            await db.execute(text("INSERT INTO tracklist_versions (id,tracklist_id,version_number) VALUES (:id,:tid,7)"), {"id": vid, "tid": tid})
            await db.execute(text("UPDATE tracklists SET latest_version_id=:vid WHERE id=:tid"), {"vid": vid, "tid": tid})
            await db.execute(
                text("INSERT INTO tracklist_tracks (id,version_id,position,timestamp) VALUES (:id,:vid,1,'12:30')"), {"id": track, "vid": vid}
            )
            await db.execute(
                text("INSERT INTO discogs_links (id,track_id,discogs_release_id,confidence,status) VALUES (:id,:track,'synthetic',0.9,'accepted')"),
                {"id": link, "track": track},
            )
            before = (
                await db.execute(
                    text("SELECT id,external_id,source_url,source,status,latest_version_id,propagated_from_set_key FROM tracklists ORDER BY id")
                )
            ).all()
        await asyncio.to_thread(upgrade_to, cfg, "085")
        async with engine.connect() as db:
            assert (
                await db.execute(
                    text("SELECT id,external_id,source_url,source,status,latest_version_id,propagated_from_set_key FROM tracklists ORDER BY id")
                )
            ).all() == before
            assert tuple((await db.execute(text("SELECT id,version_number FROM tracklist_versions"))).one()) == (vid, 7)
            assert tuple((await db.execute(text("SELECT id,artist,title,timestamp,timestamp_evidence FROM tracklist_tracks"))).one()) == (
                track,
                None,
                None,
                "12:30",
                None,
            )
            assert (await db.execute(text("SELECT id FROM discogs_links"))).scalar_one() == link
        # Unused expansion round-trips without rewriting a single legacy row.
        await asyncio.to_thread(downgrade_to, cfg, "084")
        await asyncio.to_thread(upgrade_to, cfg, "085")
        async with AsyncSession(engine) as db:
            assert await backfill_legacy_identities(db, batch_size=1) == 1
            await db.commit()
        async with AsyncSession(engine) as restarted:
            assert await backfill_legacy_identities(restarted, batch_size=1) == 0
            stored = await store_snapshot(
                restarted,
                Snapshot(
                    identity=SourceIdentity(provider_id="example_catalog", native_id="long/" + "x" * 3000),
                    retrieved_at=datetime.now(UTC),
                    source_format="txt",
                    parser_version="v1",
                    tracks=(ProviderTrack(position=1),),
                    completeness=Completeness(state="complete", reason="complete"),
                ),
            )
            # This test targets revision085, before nullable decision fields exist.
            await restarted.execute(
                text(
                    "INSERT INTO provider_selection_events(id,media_id,kind,observation_id,actor) VALUES(:id,:media,'tracklist',:observation,'reviewer')"
                ),
                {"id": uuid.uuid4(), "media": uuid.uuid4(), "observation": stored.observation.id},
            )
            observation_id = stored.observation.id
            await restarted.commit()
        async with engine.begin() as db:
            mapped = (await db.execute(text("SELECT provider_object_id FROM tracklists ORDER BY id"))).scalars().all()
            assert len(mapped) == 2 and mapped[0] == mapped[1] and mapped[0] is not None
        with pytest.raises(DBAPIError, match="immutable"):
            async with engine.begin() as db:
                await db.execute(text("UPDATE provider_source_observations SET code='overwrite' WHERE id=:id"), {"id": observation_id})
        with pytest.raises(RuntimeError, match="lossy downgrade refused"):
            await asyncio.to_thread(downgrade_to, cfg, "084")
        async with AsyncSession(engine) as db:
            assert await db.get(ProviderSourceObservation, observation_id) is not None
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
