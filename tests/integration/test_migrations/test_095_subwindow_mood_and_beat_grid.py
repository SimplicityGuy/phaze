"""Migration 095 adds ``analysis_subwindow`` and ``analysis_beat_grid`` reversibly and additively (phaze-f6eeb).

The ORM half is ``tests/shared/models/test_analysis_subwindow_beat_grid.py``. Both exist because the
migration and the model are two independent declarations of one schema
(docs/design/0012-verification-fidelity-and-operator-attribution.md, rule 3); the baseline suite's
autogenerate-drift and constraint-definition tests hold the two together.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from phaze.services.beat_grid import stored_beat_grid

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_AGENT = "INSERT INTO agents (id, name, token_hash, scan_roots) VALUES ('itest-agent', 'itest-agent', repeat('a', 64), '[\"/archive\"]')"
_FILE = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, file_type, file_size) "
    "VALUES ('00000000-0000-0000-0000-000000000001', 'itest-agent', repeat('b', 64), '/archive/<set-01>.mp3', '<set-01>.mp3', '/archive/<set-01>.mp3', 'mp3', 10)"
)
# A coarse window written before 095: its row must read back identically after upgrade and after downgrade.
_COARSE = (
    "INSERT INTO analysis_window (id, file_id, tier, window_index, start_sec, end_sec, mood, energy, mood_scores) "
    "VALUES ('00000000-0000-0000-0000-0000000000c0', '00000000-0000-0000-0000-000000000001', 'coarse', 0, 0.0, 180.0, 'happy', 0.4, "
    "'{\"mood_happy\": 0.7}'::jsonb)"
)
_COARSE_READ = "SELECT tier, window_index, start_sec, end_sec, mood, energy, mood_scores, features FROM analysis_window"
_SUBWINDOW = (
    "INSERT INTO analysis_subwindow (file_id, window_index, sub_index, start_sec, end_sec, mood_scores) "
    "VALUES ('00000000-0000-0000-0000-000000000001', :window_index, :sub_index, :start_sec, :end_sec, :mood_scores)"
)
_BEAT_GRID = "INSERT INTO analysis_beat_grid (file_id, beats, beat_count) VALUES ('00000000-0000-0000-0000-000000000001', :beats, :beat_count)"


async def _tables(connection: object) -> set[str]:
    rows = await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))  # type: ignore[attr-defined]
    return set(rows.scalars().all())


async def _refused(engine: object, statement: str, params: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:  # type: ignore[attr-defined]
            await connection.execute(text(statement), params)


async def test_095_creates_both_tables_cascades_with_files_leaves_existing_rows_alone_and_downgrades_away() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "094")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            assert {"analysis_subwindow", "analysis_beat_grid"}.isdisjoint(await _tables(connection))
            await connection.execute(text(_AGENT))
            await connection.execute(text(_FILE))
            await connection.execute(text(_COARSE))
            coarse_before = (await connection.execute(text(_COARSE_READ))).one()

        await asyncio.to_thread(upgrade_to, cfg, "095")
        mood = [i / 10 for i in range(11)]
        beats = stored_beat_grid(0.371 + i * 0.46875 for i in range(8))
        async with engine.begin() as connection:
            assert (await connection.execute(text(_COARSE_READ))).one() == coarse_before, "095 rewrote an existing analysis_window row"
            # A full sub-window and a trailing one no patch centre fell in: NULL mood, a recorded gap.
            await connection.execute(text(_SUBWINDOW), {"window_index": 0, "sub_index": 0, "start_sec": 0.0, "end_sec": 15.0, "mood_scores": mood})
            await connection.execute(text(_SUBWINDOW), {"window_index": 0, "sub_index": 1, "start_sec": 15.0, "end_sec": 16.0, "mood_scores": None})
            rows = (
                await connection.execute(
                    text("SELECT sub_index, start_sec, end_sec, mood_scores FROM analysis_subwindow ORDER BY window_index, sub_index")
                )
            ).all()
            assert [tuple(row) for row in rows] == [(0, 0.0, 15.0, mood), (1, 15.0, 16.0, None)]

            await connection.execute(text(_BEAT_GRID), {"beats": beats, "beat_count": len(beats)})
            grid = (await connection.execute(text("SELECT beats, beat_count, gaps FROM analysis_beat_grid"))).one()
            assert (grid.beats, grid.beat_count, grid.gaps) == (beats, 8, [])

        # The CHECK and key constraints refuse what no writer may produce.
        await _refused(engine, _SUBWINDOW, {"window_index": 0, "sub_index": 0, "start_sec": 0.0, "end_sec": 15.0, "mood_scores": None})
        await _refused(engine, _SUBWINDOW, {"window_index": 0, "sub_index": 2, "start_sec": 30.0, "end_sec": 30.0, "mood_scores": None})
        await _refused(engine, _SUBWINDOW, {"window_index": -1, "sub_index": 0, "start_sec": 0.0, "end_sec": 15.0, "mood_scores": None})
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM analysis_beat_grid"))
        await _refused(engine, _BEAT_GRID, {"beats": beats, "beat_count": len(beats) + 1})
        await _refused(engine, _BEAT_GRID, {"beats": [], "beat_count": -1})

        async with engine.begin() as connection:
            await connection.execute(text(_BEAT_GRID), {"beats": beats, "beat_count": len(beats)})
            await connection.execute(text("DELETE FROM analysis_window"))
            await connection.execute(text("DELETE FROM files"))
            assert (await connection.execute(text("SELECT count(*) FROM analysis_subwindow"))).scalar_one() == 0, "analysis_subwindow did not cascade"
            assert (await connection.execute(text("SELECT count(*) FROM analysis_beat_grid"))).scalar_one() == 0, "analysis_beat_grid did not cascade"
            await connection.execute(text(_FILE))
            await connection.execute(text(_COARSE))

        await asyncio.to_thread(downgrade_to, cfg, "094")
        async with engine.connect() as connection:
            assert {"analysis_subwindow", "analysis_beat_grid"}.isdisjoint(await _tables(connection))
            assert (await connection.execute(text(_COARSE_READ))).one() == coarse_before, "095's downgrade touched analysis_window"
        await asyncio.to_thread(upgrade_to, cfg, "095")
        async with engine.connect() as connection:
            assert {"analysis_subwindow", "analysis_beat_grid"} <= await _tables(connection)
    finally:
        await engine.dispose()
