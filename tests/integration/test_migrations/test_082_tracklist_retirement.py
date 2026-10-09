"""Retirement removes acquisition state while preserving stored content and provenance."""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


async def test_retirement_preserves_tracklists_and_downgrades_disarmed() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "081")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()
    track_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tracklists (id, external_id, source_url, source, auto_linked, status) "
                    "VALUES (:id, 'synthetic-set', 'https://example.invalid/set', '1001tracklists', false, 'approved')"
                ),
                {"id": tracklist_id},
            )
            await connection.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 1, NOW())"),
                {"id": version_id, "tid": tracklist_id},
            )
            await connection.execute(text("UPDATE tracklists SET latest_version_id = :id WHERE id = :tid"), {"id": version_id, "tid": tracklist_id})
            await connection.execute(
                text("INSERT INTO tracklist_tracks (id, version_id, position, title) VALUES (:id, :vid, 1, 'Opening')"),
                {"id": track_id, "vid": version_id},
            )
            await connection.execute(text("UPDATE tracklist_drain_arm_state SET armed = true, in_flight = true"))
            await connection.execute(
                text(
                    "INSERT INTO tracklist_lookup_cache (id, set_key, query_text, outcome) VALUES (gen_random_uuid(), :key, 'synthetic', 'blocked')"
                ),
                {"key": "k" * 64},
            )
        await asyncio.to_thread(upgrade_to, cfg, "082")
        async with engine.connect() as connection:
            stored = (
                await connection.execute(text("SELECT source, source_url, latest_version_id FROM tracklists WHERE id = :id"), {"id": tracklist_id})
            ).one()
            assert tuple(stored) == ("1001tracklists", "https://example.invalid/set", version_id)
            assert (await connection.execute(text("SELECT title FROM tracklist_tracks WHERE id = :id"), {"id": track_id})).scalar_one() == "Opening"
            for table in ("tracklist_lookup_cache", "tracklist_file_lookups", "tracklist_priority_flags", "tracklist_drain_arm_state"):
                assert (await connection.execute(text("SELECT to_regclass(:table)"), {"table": table})).scalar_one() is None
            default = (
                await connection.execute(
                    text("SELECT column_default FROM information_schema.columns WHERE table_name = 'tracklists' AND column_name = 'source'")
                )
            ).scalar_one()
            assert "manual" in default
        await asyncio.to_thread(downgrade_to, cfg, "081")
        async with engine.connect() as connection:
            assert tuple((await connection.execute(text("SELECT armed, in_flight FROM tracklist_drain_arm_state"))).one()) == (False, False)
            assert (await connection.execute(text("SELECT count(*) FROM tracklist_lookup_cache"))).scalar_one() == 0
        await asyncio.to_thread(upgrade_to, cfg, "082")
    finally:
        await engine.dispose()
