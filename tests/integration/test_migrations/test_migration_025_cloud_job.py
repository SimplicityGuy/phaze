"""Tests for migration 025: create the ``cloud_job`` per-file_id staging table (Phase 53, D-03).

Mirrors ``test_024.py``: static revision-id assertions run WITHOUT a DB so the
additive-only / bare-number contract holds even where Postgres is absent; the integration
body drives its own downgrade/upgrade sequence against the ephemeral migrations DB to prove
``cloud_job`` exists after 025 (with the unique FK to ``files.id`` and the status CHECK), and
that ``downgrade -1`` drops it cleanly.

CRITICAL: migration 025 must NEVER reference ``saq_jobs`` (SAQ owns that table). A grep-style
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "025_add_cloud_job.py"


def _load_migration_025() -> object:
    """Load the 025 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_025", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_TABLE_SQL = "SELECT table_name FROM information_schema.tables WHERE table_name = 'cloud_job'"


def test_revision_identifiers_are_bare_numbers() -> None:
    """025 chains off 024 using bare-number strings (no long migration names)."""
    migration_025 = _load_migration_025()
    assert migration_025.revision == "025"  # type: ignore[attr-defined]
    assert migration_025.down_revision == "024"  # type: ignore[attr-defined]
    assert migration_025.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 025 must not reference saq_jobs outside its banner: {offending}"


@pytest.mark.asyncio
async def test_upgrade_025_creates_cloud_job_then_downgrade_drops() -> None:
    """025 creates cloud_job (unique file_id FK + status CHECK); downgrade to 024 drops it."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "024")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: cloud_job absent at revision 024.
        async with engine.connect() as conn:
            before = (await conn.execute(text(_TABLE_SQL))).all()
            assert before == [], "cloud_job must not exist before migration 025"

        await asyncio.to_thread(upgrade_to, cfg, "025")

        async with engine.connect() as conn:
            after = (await conn.execute(text(_TABLE_SQL))).all()
            assert len(after) == 1, f"cloud_job must exist after 025: {after}"

        # Seed a file row to satisfy the FK, then exercise the unique FK + status CHECK.
        fid = "11111111-1111-1111-1111-111111111111"
        async with engine.begin() as wconn:
            await wconn.execute(
                text(
                    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                    "file_type, file_size, state, created_at, updated_at) "
                    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, 'awaiting_cloud', NOW(), NOW())"
                ),
                {"id": fid, "h": "abc", "p": "/music/x.flac", "n": "x.flac"},
            )
            await wconn.execute(
                text(
                    "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, :k, 'uploading', NOW(), NOW())"
                ),
                {"fid": fid, "k": f"staging/{fid}.flac"},
            )

        # Unique FK: a second cloud_job for the same file_id is rejected.
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, :k, 'uploaded', NOW(), NOW())"
                    ),
                    {"fid": fid, "k": "staging/dup"},
                )

        # status CHECK: an out-of-enum status is rejected.
        fid2 = "22222222-2222-2222-2222-222222222222"
        async with engine.begin() as wconn:
            await wconn.execute(
                text(
                    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                    "file_type, file_size, state, created_at, updated_at) "
                    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, 'awaiting_cloud', NOW(), NOW())"
                ),
                {"id": fid2, "h": "def", "p": "/music/y.flac", "n": "y.flac"},
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, :k, 'bogus', NOW(), NOW())"
                    ),
                    {"fid": fid2, "k": "staging/bad"},
                )

        # Downgrade drops the table.
        await asyncio.to_thread(downgrade_to, cfg, "024")
        async with engine.connect() as conn:
            post = (await conn.execute(text(_TABLE_SQL))).all()
            assert post == [], "downgrade to 024 must drop cloud_job"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
