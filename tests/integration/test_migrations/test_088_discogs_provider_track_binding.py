"""Real Alembic expansion preserves accepted legacy identity and refuses provider evidence loss."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from phaze.models.discogs_link import DiscogsLink

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_seeded_legacy_preservation_restart_constraints_and_used_downgrade():
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "087")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    canonical, projection, version, track, accepted, source, observation, provider_link = [uuid.uuid4() for _ in range(8)]
    try:
        async with engine.begin() as db:
            for identifier, propagation in ((canonical, None), (projection, "p" * 64)):
                await db.execute(
                    text(
                        "INSERT INTO tracklists(id,external_id,source_url,source,status,auto_linked,propagated_from_set_key) "
                        "VALUES(:id,'synthetic','','manual','approved',false,:propagation)"
                    ),
                    {"id": identifier, "propagation": propagation},
                )
            await db.execute(
                text("INSERT INTO tracklist_versions(id,tracklist_id,version_number) VALUES(:id,:canonical,7)"),
                {"id": version, "canonical": canonical},
            )
            await db.execute(text("UPDATE tracklists SET latest_version_id=:version WHERE id=:id"), {"version": version, "id": canonical})
            await db.execute(
                text("INSERT INTO tracklist_tracks(id,version_id,position,timestamp) VALUES(:id,:version,1,'12:30')"),
                {"id": track, "version": version},
            )
            await db.execute(
                text("INSERT INTO discogs_links(id,track_id,discogs_release_id,confidence,status) VALUES(:id,:track,'synthetic',95,'accepted')"),
                {"id": accepted, "track": track},
            )
            legacy_before = (await db.execute(text("SELECT * FROM discogs_links ORDER BY id"))).all()
            tracklists_before = (await db.execute(text("SELECT * FROM tracklists ORDER BY id"))).all()
        await asyncio.to_thread(upgrade_to, cfg, "088")
        # Reconnect after external Alembic DDL; asyncpg caches SELECT * result plans per connection.
        await engine.dispose()
        async with engine.connect() as db:
            after = (await db.execute(text("SELECT * FROM discogs_links ORDER BY id"))).all()
            assert tuple(after[0])[: len(legacy_before[0])] == tuple(legacy_before[0])
            assert after[0][-2:] == (None, None)
            assert (await db.execute(text("SELECT * FROM tracklists ORDER BY id"))).all() == tracklists_before
            assert tuple((await db.execute(text("SELECT id,version_number FROM tracklist_versions"))).one()) == (version, 7)
            assert tuple((await db.execute(text("SELECT id,artist,title,timestamp,timestamp_evidence FROM tracklist_tracks"))).one()) == (
                track,
                None,
                None,
                "12:30",
                None,
            )
        # Unused schema can round-trip without changing legacy UUIDs or accepted facts.
        await asyncio.to_thread(downgrade_to, cfg, "087")
        await asyncio.to_thread(upgrade_to, cfg, "088")
        await engine.dispose()
        async with engine.begin() as db:
            await db.execute(
                text("INSERT INTO provider_source_objects(id,provider_id,native_id,native_digest) VALUES(:id,'local','synthetic',:digest)"),
                {"id": source, "digest": "a" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO provider_source_observations(id,object_id,status,code,revision_scope,parser_version,content_digest,payload,truncated,retrieved_at) "
                    "VALUES(:id,:source,'found','snapshot','unknown','synthetic',:digest,'{}',false,now())"
                ),
                {"id": observation, "source": source, "digest": "b" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO discogs_links(id,source_observation_id,source_track_position,discogs_release_id,confidence,status) "
                    "VALUES(:id,:observation,1,'provider',90,'candidate')"
                ),
                {"id": provider_link, "observation": observation},
            )
        # Even a pending provider candidate is durable evidence; refusal precedes any DDL.
        with pytest.raises(RuntimeError, match="lossy downgrade refused"):
            await asyncio.to_thread(downgrade_to, cfg, "087")
        async with AsyncSession(engine) as restarted:
            retained = await restarted.get(DiscogsLink, provider_link)
            assert retained.track_id is None and retained.source_observation_id == observation and retained.source_track_position == 1
            assert (await restarted.get(DiscogsLink, accepted)).track_id == track
            assert await restarted.scalar(text("SELECT version_num FROM alembic_version")) == "088"
        for legacy_id, source_id, position in [
            (None, None, None),
            (track, observation, 1),
            (None, observation, None),
            (None, observation, 0),
            (None, observation, -1),
        ]:
            with pytest.raises(IntegrityError):
                async with engine.begin() as db:
                    await db.execute(
                        text(
                            "INSERT INTO discogs_links(id,track_id,source_observation_id,source_track_position,discogs_release_id,confidence) "
                            "VALUES(:id,:track,:observation,:position,'invalid',0)"
                        ),
                        {"id": uuid.uuid4(), "track": legacy_id, "observation": source_id, "position": position},
                    )
        async with engine.begin() as db:
            await db.execute(text("UPDATE discogs_links SET status='accepted' WHERE id=:id"), {"id": provider_link})
        with pytest.raises(IntegrityError):
            async with engine.begin() as db:
                await db.execute(
                    text(
                        "INSERT INTO discogs_links(id,source_observation_id,source_track_position,discogs_release_id,confidence,status) "
                        "VALUES(:id,:observation,1,'duplicate',90,'accepted')"
                    ),
                    {"id": uuid.uuid4(), "observation": observation},
                )
        # Different provider positions and the original legacy UUID remain independent.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO discogs_links(id,source_observation_id,source_track_position,discogs_release_id,confidence,status) "
                    "VALUES(:id,:observation,2,'different-position',90,'accepted')"
                ),
                {"id": uuid.uuid4(), "observation": observation},
            )
        with pytest.raises(RuntimeError, match="lossy downgrade refused"):
            await asyncio.to_thread(downgrade_to, cfg, "087")
        await engine.dispose()
        async with AsyncSession(engine) as restarted:
            retained = await restarted.get(DiscogsLink, provider_link)
            assert retained.status == "accepted" and retained.source_observation_id == observation and retained.source_track_position == 1
            legacy_retained = await restarted.get(DiscogsLink, accepted)
            assert legacy_retained.status == "accepted" and legacy_retained.track_id == track
            assert await restarted.scalar(text("SELECT version_num FROM alembic_version")) == "088"
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
