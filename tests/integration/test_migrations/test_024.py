"""Tests for migration 024: add the ``kind`` capability marker to the ``agents`` table.

Mirrors ``test_023.py``: static revision-id assertions run WITHOUT a DB so the
additive-only / bare-number contract holds even where Postgres is absent; the integration
body drives its own downgrade/upgrade sequence against the ephemeral migrations DB to prove
the ``kind`` column exists after 024 (NOT NULL, defaulting ``'fileserver'``), that the
pre-existing ``legacy-application-server`` row backfills to ``'fileserver'``, that the
``ck_agents_kind_enum`` CHECK rejects an out-of-enum value, and that downgrade to 023 drops it.

CRITICAL: migration 024 must NEVER reference ``saq_jobs`` (SAQ owns that table). A grep-style
assertion enforces this.

Operator pre-condition for the integration body: the database ``phaze_migrations_test`` must
exist (see ``tests/test_migrations/conftest.py``); run via ``just integration-test`` /
``just test-db``.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "024_add_agents_kind.py"


def _load_migration_024() -> object:
    """Load the 024 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_024", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_COLUMNS_SQL = (
    "SELECT column_name, is_nullable, data_type, column_default FROM information_schema.columns WHERE table_name = 'agents' AND column_name = 'kind'"
)


def test_revision_identifiers_are_bare_numbers() -> None:
    """024 chains off 023 using bare-number strings (no long migration names)."""
    migration_024 = _load_migration_024()
    assert migration_024.revision == "024"  # type: ignore[attr-defined]
    assert migration_024.down_revision == "023"  # type: ignore[attr-defined]
    assert migration_024.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 024 must not reference saq_jobs outside its banner: {offending}"


@pytest.mark.asyncio
async def test_upgrade_024_adds_kind_backfills_and_checks_then_downgrade_drops() -> None:
    """024 adds NOT NULL kind defaulting 'fileserver', backfills, rejects out-of-enum, drops on downgrade."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "023")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: kind column absent at revision 023.
        async with engine.connect() as conn:
            cols_before = (await conn.execute(text(_COLUMNS_SQL))).all()
            assert cols_before == [], "kind must not exist before migration 024"
            # The legacy agent row seeded by migration 012 is our pre-existing-row backfill subject.
            legacy = (await conn.execute(text("SELECT id FROM agents WHERE id = 'legacy-application-server'"))).all()
            assert legacy, "migration 012 must have seeded legacy-application-server before 024 runs"

        await asyncio.to_thread(upgrade_to, cfg, "024")

        async with engine.connect() as conn:
            cols_after = (await conn.execute(text(_COLUMNS_SQL))).all()
            assert len(cols_after) == 1, f"kind must exist after 024: {cols_after}"
            name, is_nullable, data_type, column_default = cols_after[0]
            assert name == "kind"
            assert is_nullable == "NO"
            assert data_type == "character varying"
            assert column_default is not None and "fileserver" in column_default

            # Backfill: the pre-existing legacy row now reads 'fileserver' via the server_default.
            legacy_kind = (await conn.execute(text("SELECT kind FROM agents WHERE id = 'legacy-application-server'"))).scalar_one()
            assert legacy_kind == "fileserver"

        # A valid 'compute' row is accepted.
        async with engine.begin() as wconn:
            await wconn.execute(
                text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :name, :kind, NOW(), NOW())"),
                {"id": "compute-agent", "name": "compute-agent", "kind": "compute"},
            )

        # CHECK reject: ck_agents_kind_enum rejects an out-of-enum value.
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :name, :kind, NOW(), NOW())"),
                    {"id": "bogus-agent", "name": "bogus-agent", "kind": "bogus"},
                )

        # Downgrade drops the kind column (and its constraint).
        await asyncio.to_thread(downgrade_to, cfg, "023")
        async with engine.connect() as conn:
            cols_post_downgrade = (await conn.execute(text(_COLUMNS_SQL))).all()
            assert cols_post_downgrade == [], "downgrade to 023 must drop the kind column"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
