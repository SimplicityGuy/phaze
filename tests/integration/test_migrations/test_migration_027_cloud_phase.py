"""Tests for migration 027: add the ``cloud_phase`` admission column to ``cloud_job`` (Phase 55, D-04).

Mirrors ``test_migration_026_kube_columns.py``: static revision-id assertions run WITHOUT a DB so
the additive-only / bare-number contract holds even where Postgres is absent; the integration body
upgrades to 026 then 027, proves the new ``cloud_phase`` column exists, that the CHECK accepts the
four admission-progression members (queued_behind_quota / admitted / running / finished) and a NULL
(a1/local rows stay NULL) while rejecting an unknown value, then downgrades to 026 and proves the
column + CHECK are gone.

CRITICAL: migration 027 must NEVER reference ``saq_jobs`` (SAQ owns that table). A grep-style
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "027_add_cloud_job_cloud_phase.py"


def _load_migration_027() -> object:
    """Load the 027 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_027", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_COLUMN_SQL = "SELECT column_name FROM information_schema.columns WHERE table_name = 'cloud_job' AND column_name = 'cloud_phase'"


def test_revision_identifiers_are_bare_numbers() -> None:
    """027 chains off 026 using bare-number strings (no long migration names)."""
    migration_027 = _load_migration_027()
    assert migration_027.revision == "027"  # type: ignore[attr-defined]
    assert migration_027.down_revision == "026"  # type: ignore[attr-defined]
    assert migration_027.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 027 must not reference saq_jobs outside its banner: {offending}"


async def _seed_file(engine, fid: str, suffix: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row so the cloud_job FK is satisfiable."""
    async with engine.begin() as wconn:
        await wconn.execute(
            text(
                "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
                "file_type, file_size, state, created_at, updated_at) "
                "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, 'awaiting_cloud', NOW(), NOW())"
            ),
            {"id": fid, "h": suffix, "p": f"/music/{suffix}.flac", "n": f"{suffix}.flac"},
        )


@pytest.mark.asyncio
async def test_upgrade_027_adds_cloud_phase_then_downgrade_drops() -> None:
    """027 adds cloud_phase + CHECK; accepts the 4 members and NULL, rejects bogus; downgrade reverses cleanly."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "026")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: cloud_phase is absent at revision 026.
        async with engine.connect() as conn:
            before = {row[0] for row in (await conn.execute(text(_COLUMN_SQL))).all()}
            assert before == set(), f"cloud_phase must not exist before migration 027: {before}"

        await asyncio.to_thread(upgrade_to, cfg, "027")

        # The column now exists.
        async with engine.connect() as conn:
            after = {row[0] for row in (await conn.execute(text(_COLUMN_SQL))).all()}
            assert after == {"cloud_phase"}, f"cloud_phase must exist after 027: {after}"

        # A row with cloud_phase=NULL succeeds (a1/local rows stay NULL).
        fid_null = "00000000-0000-0000-0000-000000000000"
        await _seed_file(engine, fid_null, "null")
        async with engine.begin() as wconn:
            await wconn.execute(
                text(
                    "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :fid, :k, 'submitted', NOW(), NOW())"
                ),
                {"fid": fid_null, "k": "staging/null"},
            )
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT cloud_phase FROM cloud_job WHERE file_id = :fid"), {"fid": fid_null})).one()
            assert row[0] is None  # admission phase is k8s-only; NULL by default

        # Each of the four admission-progression members is accepted.
        for idx, phase in enumerate(("queued_behind_quota", "admitted", "running", "finished")):
            fid = f"11111111-1111-1111-1111-11111111110{idx}"
            await _seed_file(engine, fid, f"ph{idx}")
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, cloud_phase, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :fid, :k, 'submitted', :ph, NOW(), NOW())"
                    ),
                    {"fid": fid, "k": f"staging/{phase}", "ph": phase},
                )

        # An out-of-enum cloud_phase is rejected by the CHECK.
        fid_bad = "33333333-3333-3333-3333-333333333333"
        await _seed_file(engine, fid_bad, "bad")
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, cloud_phase, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :fid, :k, 'submitted', 'bogus', NOW(), NOW())"
                    ),
                    {"fid": fid_bad, "k": "staging/bad"},
                )

        # Clear the seeded rows -- the teardown downgrade chain re-narrows the status CHECK at 026->025,
        # which the surviving 'submitted' rows would violate (a real downgrade drains in-flight work first).
        async with engine.begin() as wconn:
            await wconn.execute(text("DELETE FROM cloud_job"))

        # Downgrade to 026: column gone (so the CHECK is gone with it).
        await asyncio.to_thread(downgrade_to, cfg, "026")
        async with engine.connect() as conn:
            post = {row[0] for row in (await conn.execute(text(_COLUMN_SQL))).all()}
            assert post == set(), f"downgrade to 026 must drop cloud_phase: {post}"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
