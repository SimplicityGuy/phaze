"""Migration 063 (phaze-x1qr3.1): the set projection round-trips and rewrites nothing.

The bead's blast-radius statement is that this migration changes the schema for every file in
the corpus while leaving every read of ``analysis_window`` working. The claim that carries that
is "purely additive": three NULLABLE columns and one new table, no rewrite. The tests below are
what discharge it rather than the prose:

* ``test_upgrade_adds_the_projection_columns_and_table`` -- 062 -> 063 adds exactly the three
  columns and the one table, and the columns land NULLABLE.
* ``test_upgrade_leaves_existing_analysis_window_rows_untouched`` -- the load-bearing one. Rows
  written BEFORE 063 are read back after it with the same count and every pre-existing non-null
  value byte-for-byte identical, and the three new columns NULL. A migration that rewrote,
  defaulted or dropped a row fails here.
* ``test_downgrade_removes_them_and_still_leaves_the_rows_untouched`` -- 063 -> 062 is clean,
  and the same rows survive the reverse direction too.
* ``test_set_profile_cascades_with_its_file`` -- the FK's ON DELETE CASCADE at the DATABASE
  level, which is what actually removes the row (its ORM half is
  ``tests/shared/models/test_set_profile.py``).

Ordering note: these drive Alembic themselves rather than taking the ``migrated_engine``
fixture, because every one of them needs the DB at 062 first. They share the migrations
harness (``MIGRATIONS_TEST_DATABASE_URL``) and reset the schema on the way out, exactly as
``test_upgrade_downgrade_roundtrip`` in the baseline suite does.
"""

import asyncio
from collections.abc import AsyncGenerator
import uuid

from alembic.config import Config
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    _reset_schema,
    downgrade_to,
    upgrade_to,
)


_PREVIOUS_REVISION = "062"
_THIS_REVISION = "063"
_NEW_WINDOW_COLUMNS = frozenset({"energy", "camelot", "mood_scores"})

# One fine-tier and one coarse-tier row, carrying the columns each tier actually populates. These
# are the rows the "untouched" assertions are made against, so they deliberately exercise both
# shapes -- a migration that only preserved one tier would pass a single-row test.
_FINE_ROW = {
    "tier": "fine",
    "window_index": 0,
    "start_sec": 0.0,
    "end_sec": 30.0,
    "bpm": 128.5,
    "musical_key": "A minor",
    "mood": None,
    "style": None,
    "danceability": None,
}
_COARSE_ROW = {
    "tier": "coarse",
    "window_index": 0,
    "start_sec": 0.0,
    "end_sec": 180.0,
    "bpm": None,
    "musical_key": None,
    "mood": "party",
    "style": "electronic",
    "danceability": 0.75,
}
_PRE_EXISTING_ROWS = (_FINE_ROW, _COARSE_ROW)
# Every column that exists at 062 and must read back identically at 063.
_PRESERVED_COLUMNS = ("tier", "window_index", "start_sec", "end_sec", "bpm", "musical_key", "mood", "style", "danceability")


@pytest_asyncio.fixture
async def engine_at_previous_revision() -> AsyncGenerator[tuple[AsyncEngine, Config]]:
    """Reset the migrations DB, upgrade it to 062, and yield an engine plus the alembic config.

    ``upgrade_to`` / ``downgrade_to`` are sync and internally ``asyncio.run`` alembic's async
    env, so every call is wrapped in ``asyncio.to_thread`` -- calling them directly from an async
    fixture raises "cannot be called from a running event loop" (see the conftest's own note on
    ``migrated_engine``).
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


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"),
            {"t": table},
        )
    return found.scalar_one_or_none() is not None


async def _seed_file(engine: AsyncEngine) -> uuid.UUID:
    """Insert one agent + one file and return the file id (the FK anchor for everything below)."""
    agent_id, file_id = f"projection-agent-{uuid.uuid4().hex[:8]}", uuid.uuid4()
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


async def _seed_pre_063_windows(engine: AsyncEngine, file_id: uuid.UUID) -> None:
    """Insert the pre-existing window rows using ONLY the columns that exist at 062."""
    async with engine.begin() as conn:
        for row in _PRE_EXISTING_ROWS:
            await conn.execute(
                text(
                    "INSERT INTO analysis_window "
                    "(id, file_id, tier, window_index, start_sec, end_sec, bpm, musical_key, mood, style, danceability) "
                    "VALUES (:id, :file_id, :tier, :window_index, :start_sec, :end_sec, :bpm, :musical_key, :mood, :style, :danceability)"
                ),
                {"id": uuid.uuid4(), "file_id": file_id, **row},
            )


async def _read_back_windows(engine: AsyncEngine, file_id: uuid.UUID) -> list[dict[str, object]]:
    """Read the seeded rows back, ordered, projecting only the columns that existed at 062."""
    columns = ", ".join(_PRESERVED_COLUMNS)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(f"SELECT {columns} FROM analysis_window WHERE file_id = :file_id ORDER BY tier, window_index"),  # noqa: S608 - constant column list
            {"file_id": file_id},
        )
        return [dict(m) for m in rows.mappings().all()]


@pytest.mark.asyncio
async def test_upgrade_adds_the_projection_columns_and_table(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """062 -> 063 adds exactly energy/camelot/mood_scores (nullable) and the set_profile table."""
    engine, cfg = engine_at_previous_revision
    before = await _analysis_window_columns(engine)
    assert _NEW_WINDOW_COLUMNS.isdisjoint(before), f"columns already present at {_PREVIOUS_REVISION}: {sorted(_NEW_WINDOW_COLUMNS & set(before))}"
    assert not await _table_exists(engine, "set_profile")

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    assert await _current_revision(engine) == _THIS_REVISION
    after = await _analysis_window_columns(engine)
    assert set(after) - set(before) == _NEW_WINDOW_COLUMNS, "063 added a column beyond the three the bead declares"
    # NULLABLE is the whole premise: a NOT NULL column would force a rewrite of every existing row.
    assert {after[name] for name in _NEW_WINDOW_COLUMNS} == {"YES"}
    assert await _table_exists(engine, "set_profile")


@pytest.mark.asyncio
async def test_upgrade_leaves_existing_analysis_window_rows_untouched(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """Rows written at 062 survive the upgrade with the same count and the same non-null values.

    This is the acceptance criterion the bead's "no existing row is rewritten and no existing
    read changes" rests on. The comparison is over every column that existed at 062, so a
    migration that defaulted, coerced or re-typed one of them fails here rather than in
    production.
    """
    engine, cfg = engine_at_previous_revision
    file_id = await _seed_file(engine)
    await _seed_pre_063_windows(engine, file_id)
    before = await _read_back_windows(engine, file_id)
    assert len(before) == len(_PRE_EXISTING_ROWS)

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    after = await _read_back_windows(engine, file_id)
    assert after == before, "063 changed a pre-existing analysis_window value"
    # ... and the three new columns read NULL, which is the honest "not yet projected" the lanes
    # render as a gap. phaze-x1qr3.3 is what fills them, from stored JSONB and with no re-analysis.
    async with engine.connect() as conn:
        projected = await conn.execute(
            text("SELECT energy, camelot, mood_scores FROM analysis_window WHERE file_id = :file_id"),
            {"file_id": file_id},
        )
        assert [tuple(row) for row in projected.all()] == [(None, None, None)] * len(_PRE_EXISTING_ROWS)


@pytest.mark.asyncio
async def test_downgrade_removes_them_and_still_leaves_the_rows_untouched(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """063 -> 062 drops the three columns and the table, and the pre-existing rows still match."""
    engine, cfg = engine_at_previous_revision
    file_id = await _seed_file(engine)
    await _seed_pre_063_windows(engine, file_id)
    before = await _read_back_windows(engine, file_id)

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    await asyncio.to_thread(downgrade_to, cfg, _PREVIOUS_REVISION)

    assert await _current_revision(engine) == _PREVIOUS_REVISION
    columns = await _analysis_window_columns(engine)
    assert _NEW_WINDOW_COLUMNS.isdisjoint(columns), f"downgrade left projection columns behind: {sorted(_NEW_WINDOW_COLUMNS & set(columns))}"
    assert not await _table_exists(engine, "set_profile"), "downgrade left set_profile behind"
    assert await _read_back_windows(engine, file_id) == before, "the downgrade changed a pre-existing analysis_window value"


@pytest.mark.asyncio
async def test_set_profile_cascades_with_its_file(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """The FK is ON DELETE CASCADE in the DATABASE, so a profile can never block a file delete.

    Asserted against real DDL rather than the ORM mapping: ``services/scan_deletion.py`` deletes
    file sidecars with raw ``DELETE`` statements and deliberately OMITS the ones whose FK cascades
    (``analysis_window`` today, ``set_profile`` now). If the cascade were only an ORM-level
    ``cascade="all, delete-orphan"``, that raw delete would raise ForeignKeyViolation and make the
    batch permanently undeletable -- the stage_skip defect this repo has already paid for once.
    """
    engine, cfg = engine_at_previous_revision
    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    file_id = await _seed_file(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO set_profile (file_id, camelot_modal, peak_sec, projection_version) VALUES (:file_id, '8A', 42.0, 1)"),
            {"file_id": file_id},
        )

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM files WHERE id = :file_id"), {"file_id": file_id})

    async with engine.connect() as conn:
        remaining = await conn.execute(text("SELECT COUNT(*) FROM set_profile WHERE file_id = :file_id"), {"file_id": file_id})
        assert remaining.scalar_one() == 0


@pytest.mark.asyncio
async def test_set_profile_is_one_row_per_file(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """``file_id`` is the primary key, so a second profile for the same file is rejected.

    The 1:1-ness is the reason this is a separate table from ``analysis`` rather than a general
    sidecar, and a duplicate row would silently give every later surface two different pictures of
    the same set to choose between.
    """
    engine, cfg = engine_at_previous_revision
    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    file_id = await _seed_file(engine)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO set_profile (file_id, projection_version) VALUES (:file_id, 1)"), {"file_id": file_id})

    with pytest.raises(IntegrityError, match="pk_set_profile"):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO set_profile (file_id, projection_version) VALUES (:file_id, 2)"), {"file_id": file_id})
