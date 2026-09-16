"""Migration 065 (phaze-6r3eh): drops ``analysis_window.camelot`` -- the inverse of 063's half.

Operator decision, 2026-09-16 (bead ``phaze-6r3eh``, ``docs/design/0018-set-projection-and-file-viewer.md``):
drop the column; ``AnalysisWindow.camelot`` becomes a read-time property of ``musical_key``
(:func:`phaze.services.set_projection.camelot_code`), so every reader gets the identical value
with zero backfill. ``energy`` and ``mood_scores`` -- the other two columns 063 added -- are
untouched by this migration; only the ``camelot`` half is reversed.

* ``test_upgrade_drops_only_the_camelot_column`` -- 064 -> 065 removes ``camelot`` and leaves
  ``energy``/``mood_scores`` (and everything else on the table) exactly as they were.
* ``test_upgrade_leaves_musical_key_and_other_columns_untouched`` -- rows written BEFORE 065,
  including a populated ``camelot`` value that matches its own ``musical_key``, are read back
  after the upgrade with every SURVIVING column byte-identical and ``musical_key`` intact -- the
  one column the read-time property depends on to reconstruct the dropped value.
* ``test_downgrade_recreates_the_column_nullable_and_does_not_backfill`` -- 065 -> 064 adds
  ``camelot`` back as nullable, and it reads NULL even for a row whose ``musical_key`` a
  read-time property would happily resolve -- the module docstring's "does NOT backfill" claim.
"""

import asyncio
from collections.abc import AsyncGenerator
import uuid

from alembic.config import Config
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    _reset_schema,
    downgrade_to,
    upgrade_to,
)


_PREVIOUS_REVISION = "064"
_THIS_REVISION = "065"

# The pre-existing row this migration must leave alone apart from `camelot`. `camelot` here is
# deliberately consistent with `musical_key` ("A minor" -> "8A"), matching what the writer would
# have stored before this bead and what the read-time property now computes on demand.
_FINE_ROW = {
    "tier": "fine",
    "window_index": 0,
    "start_sec": 0.0,
    "end_sec": 30.0,
    "bpm": 128.5,
    "musical_key": "A minor",
    "energy": None,
    "camelot": "8A",
    "mood_scores": None,
}
_COARSE_ROW = {
    "tier": "coarse",
    "window_index": 0,
    "start_sec": 0.0,
    "end_sec": 180.0,
    "bpm": None,
    "musical_key": None,
    "energy": 0.42,
    "camelot": None,
    "mood_scores": None,
}
_PRE_EXISTING_ROWS = (_FINE_ROW, _COARSE_ROW)
_SURVIVING_COLUMNS = ("tier", "window_index", "start_sec", "end_sec", "bpm", "musical_key", "energy", "mood_scores")


@pytest_asyncio.fixture
async def engine_at_previous_revision() -> AsyncGenerator[tuple[AsyncEngine, Config]]:
    """Reset the migrations DB, upgrade it to 064, and yield an engine plus the alembic config.

    Mirrors ``test_063_set_projection.py``'s fixture of the same name and the same reasoning:
    ``upgrade_to``/``downgrade_to`` are sync and internally ``asyncio.run`` alembic's async env,
    so every call is wrapped in ``asyncio.to_thread``.
    """
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, _PREVIOUS_REVISION)
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        yield engine, cfg
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


async def _current_revision(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())


async def _analysis_window_columns(engine: AsyncEngine) -> dict[str, str]:
    """Return ``{column_name: is_nullable}`` for ``analysis_window``."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'analysis_window'"),
        )
    return dict(rows.all())  # type: ignore[arg-type]


async def _seed_file(engine: AsyncEngine) -> uuid.UUID:
    """Insert one agent + one file and return the file id (the FK anchor for everything below)."""
    agent_id, file_id = f"drop-camelot-agent-{uuid.uuid4().hex[:8]}", uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                "VALUES (:id, :sha, :path, 'set.mp3', :path, 'mp3', 1024, :agent_id)"
            ),
            {"id": file_id, "sha": uuid.uuid4().hex * 2, "path": f"/test/music/{file_id}/set.mp3", "agent_id": agent_id},
        )
    return file_id


async def _seed_pre_065_windows(engine: AsyncEngine, file_id: uuid.UUID) -> None:
    """Insert the pre-existing window rows using the columns that exist at 064 (063's three
    projection columns included)."""
    async with engine.begin() as conn:
        for row in _PRE_EXISTING_ROWS:
            await conn.execute(
                text(
                    "INSERT INTO analysis_window "
                    "(id, file_id, tier, window_index, start_sec, end_sec, bpm, musical_key, energy, camelot, mood_scores) "
                    "VALUES (:id, :file_id, :tier, :window_index, :start_sec, :end_sec, :bpm, :musical_key, :energy, :camelot, :mood_scores)"
                ),
                {"id": uuid.uuid4(), "file_id": file_id, **row},
            )


async def _read_back_surviving_columns(engine: AsyncEngine, file_id: uuid.UUID) -> list[dict[str, object]]:
    """Read the seeded rows back, ordered, projecting only the columns 065 does not touch."""
    columns = ", ".join(_SURVIVING_COLUMNS)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(f"SELECT {columns} FROM analysis_window WHERE file_id = :file_id ORDER BY tier, window_index"),  # noqa: S608 - constant column list
            {"file_id": file_id},
        )
        return [dict(m) for m in rows.mappings().all()]


@pytest.mark.asyncio
async def test_upgrade_drops_only_the_camelot_column(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """064 -> 065 removes exactly ``camelot``; ``energy``/``mood_scores`` and everything else stay."""
    engine, cfg = engine_at_previous_revision
    before = await _analysis_window_columns(engine)
    assert "camelot" in before
    assert {"energy", "mood_scores"} <= set(before)

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    assert await _current_revision(engine) == _THIS_REVISION
    after = await _analysis_window_columns(engine)
    assert "camelot" not in after
    assert set(before) - set(after) == {"camelot"}, "065 removed a column beyond camelot"
    assert {"energy", "mood_scores"} <= set(after)


@pytest.mark.asyncio
async def test_upgrade_leaves_musical_key_and_other_columns_untouched(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """Rows written at 064 survive the upgrade with the same count and every surviving value
    byte-identical -- ``musical_key`` above all, since the read-time property depends on it."""
    engine, cfg = engine_at_previous_revision
    file_id = await _seed_file(engine)
    await _seed_pre_065_windows(engine, file_id)
    before = await _read_back_surviving_columns(engine, file_id)
    assert len(before) == len(_PRE_EXISTING_ROWS)

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    after = await _read_back_surviving_columns(engine, file_id)
    assert after == before, "065 changed a pre-existing analysis_window value it should not touch"

    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'analysis_window'"))
        assert "camelot" not in {row[0] for row in result.all()}


@pytest.mark.asyncio
async def test_downgrade_recreates_the_column_nullable_and_does_not_backfill(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """065 -> 064 adds ``camelot`` back NULLABLE, and does NOT recompute it from ``musical_key``.

    The row seeded here has ``musical_key="A minor"`` -- a value a read-time property (or a
    backfill) could trivially resolve to "8A" -- specifically so this test can tell "recreated but
    empty" apart from "recreated and silently repopulated".
    """
    engine, cfg = engine_at_previous_revision
    file_id = await _seed_file(engine)
    await _seed_pre_065_windows(engine, file_id)

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    await asyncio.to_thread(downgrade_to, cfg, _PREVIOUS_REVISION)

    assert await _current_revision(engine) == _PREVIOUS_REVISION
    columns = await _analysis_window_columns(engine)
    assert columns.get("camelot") == "YES", "downgrade must recreate camelot as NULLABLE"

    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT musical_key, camelot FROM analysis_window WHERE file_id = :file_id ORDER BY tier, window_index"),
            {"file_id": file_id},
        )
        results = rows.all()
    # ORDER BY tier sorts "coarse" before "fine" alphabetically, so the coarse row (no key) comes
    # first and the fine row ("A minor") second -- the reverse of `_PRE_EXISTING_ROWS`' own order.
    assert [r.musical_key for r in results] == [None, "A minor"], "musical_key must survive the round trip"
    assert [r.camelot for r in results] == [None, None], "downgrade must NOT backfill camelot from musical_key"
