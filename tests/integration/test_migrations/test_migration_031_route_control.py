"""Tests for migration 031: add ``route_control`` + seed the single ``'global'`` row (Phase 71, BEUI-02, D-09).

Mirrors ``test_migration_030_staging_bucket.py``: static revision-id assertions run WITHOUT a DB so the
additive-only / bare-number contract holds even where Postgres is absent; the integration body upgrades
030 -> 031, proves the ``route_control`` table exists with EXACTLY the single seeded ``id='global'`` row
(``force_local`` false), then downgrades to 030 and proves the table is gone.

D-09: ``route_control`` is a one-row control table (PK ``id`` default ``'global'``, ``force_local`` bool
default false) persisting the force-local override, mirroring the ``pipeline_stage_control`` pattern.

CRITICAL: migration 031 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/030 banner). A
grep-style assertion enforces this.

Operator pre-condition for the integration body: the database ``phaze_migrations_test`` must exist
(see ``tests/integration/test_migrations/conftest.py``); run via ``just integration-test`` / ``just test-db``.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "031_add_route_control.py"


def _load_migration_031() -> object:
    """Load the 031 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_031", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_ROUTE_CONTROL_TABLE_SQL = "SELECT table_name FROM information_schema.tables WHERE table_name = 'route_control'"


def test_revision_identifiers_are_bare_numbers() -> None:
    """031 chains off 030 using bare-number strings (no long migration names)."""
    migration_031 = _load_migration_031()
    assert migration_031.revision == "031"  # type: ignore[attr-defined]
    assert migration_031.down_revision == "030"  # type: ignore[attr-defined]
    assert migration_031.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/030 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 031 must not reference saq_jobs outside its banner: {offending}"


@pytest.mark.asyncio
async def test_upgrade_031_creates_seeded_route_control_then_downgrade_reverses() -> None:
    """031 creates ``route_control`` with the single seeded ``id='global'`` (force_local false) row; downgrade drops it."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "030")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition at 030: route_control absent.
        async with engine.connect() as conn:
            before = {row[0] for row in (await conn.execute(text(_ROUTE_CONTROL_TABLE_SQL))).all()}
            assert before == set(), f"route_control must not exist before migration 031: {before}"

        await asyncio.to_thread(upgrade_to, cfg, "031")

        # route_control now exists with EXACTLY one seeded row: id='global', force_local=false.
        async with engine.connect() as conn:
            after = {row[0] for row in (await conn.execute(text(_ROUTE_CONTROL_TABLE_SQL))).all()}
            assert after == {"route_control"}, f"route_control must exist after 031: {after}"

            rows = (await conn.execute(text("SELECT id, force_local FROM route_control"))).all()
            assert len(rows) == 1, f"exactly one seeded row expected: {rows}"
            assert rows[0][0] == "global", f"seeded row id must be 'global': {rows[0]}"
            assert rows[0][1] is False, f"seeded row force_local must be false: {rows[0]}"

        # Downgrade to 030: route_control gone.
        await asyncio.to_thread(downgrade_to, cfg, "030")
        async with engine.connect() as conn:
            post = {row[0] for row in (await conn.execute(text(_ROUTE_CONTROL_TABLE_SQL))).all()}
            assert post == set(), f"downgrade to 030 must drop route_control: {post}"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
