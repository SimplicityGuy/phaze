"""Migration 072 (phaze-mvq8z.6): creates ``runtime_config_override``, the DB-override layer.

Purely additive: one new standalone table, no changes to any existing one, no seed rows (a key
with no row here simply falls through to the ``file``/``env``/``default`` layers underneath it --
see ``phaze.runtime_config``). The tests below discharge this bead's "migration up/down tested"
acceptance criterion directly, rather than leaving it to prose:

* ``test_upgrade_creates_the_table`` -- 071 -> 072 creates ``runtime_config_override`` with the
  expected columns/types and no rows.
* ``test_upgrade_round_trips_a_written_row`` -- a row written after the upgrade (mirroring how the
  admin API writes one) reads back with its JSONB value intact, ints and strings both.
* ``test_downgrade_drops_the_table`` -- 072 -> 071 removes the table outright; this molecule lands
  as a single ``--no-ff`` merge to ``main`` and is meant to be revertible with one
  ``git revert -m 1`` (epic ``phaze-mvq8z`` comment, 2026-09-28), so the downgrade must genuinely
  work, not merely exist.
"""

import asyncio
from collections.abc import AsyncGenerator

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


_PREVIOUS_REVISION = "071"
_THIS_REVISION = "072"
_TABLE = "runtime_config_override"


@pytest_asyncio.fixture
async def engine_at_previous_revision() -> AsyncGenerator[tuple[AsyncEngine, Config]]:
    """Reset the migrations DB, upgrade it to 071, and yield an engine plus the alembic config.

    Mirrors ``test_066_drop_analysis_window_camelot.py``'s fixture of the same name:
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


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"), {"t": table})
    return found.scalar_one_or_none() is not None


async def _columns(engine: AsyncEngine, table: str) -> dict[str, str]:
    """Return ``{column_name: data_type}`` for ``table``."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"),
            {"t": table},
        )
    return dict(rows.all())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_upgrade_creates_the_table(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """071 -> 072 creates ``runtime_config_override`` with the expected columns and no rows."""
    engine, cfg = engine_at_previous_revision
    assert not await _table_exists(engine, _TABLE)

    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    assert await _current_revision(engine) == _THIS_REVISION
    assert await _table_exists(engine, _TABLE)
    columns = await _columns(engine, _TABLE)
    assert columns["key"] == "character varying"
    assert columns["value"] == "jsonb"
    assert columns["created_at"] == "timestamp with time zone"
    assert columns["updated_at"] == "timestamp with time zone"
    async with engine.connect() as conn:
        count = await conn.execute(text(f"SELECT COUNT(*) FROM {_TABLE}"))  # noqa: S608 - constant table name
    assert count.scalar_one() == 0


@pytest.mark.asyncio
async def test_upgrade_round_trips_a_written_row(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """A row written post-upgrade (int and string values both) reads back byte-identical."""
    engine, cfg = engine_at_previous_revision
    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)

    async with engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO {_TABLE} (key, value) VALUES ('worker_max_jobs', '4'), ('log_level', '\"DEBUG\"')"),  # noqa: S608
        )

    async with engine.connect() as conn:
        rows = await conn.execute(text(f"SELECT key, value FROM {_TABLE} ORDER BY key"))  # noqa: S608
        results = dict(rows.all())
    assert results == {"worker_max_jobs": 4, "log_level": "DEBUG"}


@pytest.mark.asyncio
async def test_downgrade_drops_the_table(engine_at_previous_revision: tuple[AsyncEngine, Config]) -> None:
    """072 -> 071 drops the table outright, losing only override rows (never anything else)."""
    engine, cfg = engine_at_previous_revision
    await asyncio.to_thread(upgrade_to, cfg, _THIS_REVISION)
    async with engine.begin() as conn:
        await conn.execute(text(f"INSERT INTO {_TABLE} (key, value) VALUES ('worker_max_jobs', '4')"))  # noqa: S608

    await asyncio.to_thread(downgrade_to, cfg, _PREVIOUS_REVISION)

    assert await _current_revision(engine) == _PREVIOUS_REVISION
    assert not await _table_exists(engine, _TABLE)
