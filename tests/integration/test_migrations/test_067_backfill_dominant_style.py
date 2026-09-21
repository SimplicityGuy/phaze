"""Migration 067 restores ranked scores and the modal label from coarse windows."""

import asyncio
from collections.abc import AsyncGenerator
import json
import uuid

from alembic.config import Config
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, upgrade_to


@pytest_asyncio.fixture
async def engine_at_066() -> AsyncGenerator[tuple[AsyncEngine, Config]]:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "066")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        yield engine, cfg
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_uses_duration_and_first_window_tie_break(engine_at_066: tuple[AsyncEngine, Config]) -> None:
    engine, cfg = engine_at_066
    agent_id = f"style-agent-{uuid.uuid4().hex[:8]}"
    long_file, tied_file = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"), {"id": agent_id}
        )
        for file_id in (long_file, tied_file):
            await conn.execute(
                text(
                    "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                    "VALUES (:id, :sha, :path, 'song.mp3', :path, 'mp3', 1024, :agent_id)"
                ),
                {"id": file_id, "sha": uuid.uuid4().hex * 2, "path": f"/test/music/{file_id}/song.mp3", "agent_id": agent_id},
            )
            await conn.execute(
                text("INSERT INTO analysis (id, file_id, style) VALUES (:id, :file_id, :style)"),
                {"id": uuid.uuid4(), "file_id": file_id, "style": "Electronic/Psy-Trance=0.66,Electronic/Progressive House="[:50]},
            )
        for file_id, index, start, end, style, scores in (
            (long_file, 0, 0.0, 60.0, "Electronic/Psy-Trance", {"Electronic/Psy-Trance": 0.9, "Electronic/House": 0.1}),
            (long_file, 1, 60.0, 180.0, "Electronic/House", {"Electronic/Psy-Trance": 0.1, "Electronic/House": 0.8}),
            (long_file, 2, 180.0, 210.0, "Electronic/Psy-Trance", {"Electronic/Psy-Trance": 0.9, "Electronic/House": 0.1}),
            (tied_file, 0, 0.0, 60.0, "Rock", {"Rock": 0.8, "Pop": 0.2}),
            (tied_file, 1, 60.0, 120.0, "Pop", {"Rock": 0.2, "Pop": 0.8}),
        ):
            await conn.execute(
                text(
                    "INSERT INTO analysis_window (id, file_id, tier, window_index, start_sec, end_sec, style, features) "
                    "VALUES (:id, :file_id, 'coarse', :index, :start, :end, :style, CAST(:features AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "file_id": file_id,
                    "index": index,
                    "start": start,
                    "end": end,
                    "style": style,
                    "features": json.dumps({"genre": {"predictions": [{"label": name, "confidence": score} for name, score in scores.items()]}}),
                },
            )

    await asyncio.to_thread(upgrade_to, cfg, "067")
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT file_id, dominant_style, style FROM analysis WHERE file_id IN (:long_file, :tied_file)"),
            {"long_file": long_file, "tied_file": tied_file},
        )
    actual = {file_id: (dominant, styles) for file_id, dominant, styles in rows.all()}
    assert actual[long_file][0] == "Electronic/House"
    assert actual[long_file][1][0]["name"] == "Electronic/House"
    assert actual[long_file][1][0]["score"] == pytest.approx(0.5)
    assert actual[tied_file][0] == "Rock"
    assert actual[tied_file][1][0]["name"] == "Pop"
    assert actual[tied_file][1][0]["score"] == pytest.approx(0.5)

    # A containment predicate uses ix_analysis_style_gin to shortlist files by
    # any predicted style; the score is filtered within the matching object.
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT file_id FROM analysis WHERE style @> CAST(:needle AS jsonb) "
                "AND EXISTS (SELECT 1 FROM jsonb_array_elements(style) AS candidate "
                "WHERE candidate->>'name' = :name AND (candidate->>'score')::float >= :minimum)"
            ),
            {"needle": '[{"name":"Electronic/House"}]', "name": "Electronic/House", "minimum": 0.5},
        )
    assert result.scalars().all() == [long_file]
