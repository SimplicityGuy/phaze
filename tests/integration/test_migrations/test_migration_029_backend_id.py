"""Tests for migration 029: add ``cloud_job.backend_id`` + make ``s3_key`` nullable (Phase 68, D-06/D-08).

Mirrors ``test_migration_026_kube_columns.py``: static revision-id assertions run WITHOUT a DB so the
additive-only / bare-number contract holds even where Postgres is absent; the integration body
upgrades 028 -> 029, proves ``backend_id`` exists (nullable, no backfill) AND ``s3_key`` is now
nullable (a compute-shaped row with ``s3_key = NULL`` inserts), then downgrades to 028 and proves the
column is gone and ``s3_key`` is ``NOT NULL`` again.

GUARDED SCAFFOLD. Migration ``029_add_cloud_job_backend_id.py`` lands in Wave 1; until then a
file-exists ``skipif`` guards every test so this file COLLECTS cleanly in Wave 0 and lights up
automatically once the migration appears.

D-06: ``backend_id`` is a plain nullable ``String`` -- NO CHECK/enum change (unlike 026's status-CHECK
swap) and NO backfill (the a1/k8s paths were never deployed; ``backend_id`` is config-derived and
stamped at dispatch going forward).
D-08: ``s3_key`` becomes nullable because a compute burst has no S3 object.

CRITICAL: migration 029 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/026 banner). A
grep-style assertion enforces this.

Operator pre-condition for the integration body: the database ``phaze_migrations_test`` must exist
(see ``tests/integration/test_migrations/conftest.py``); run via ``just integration-test`` /
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "029_add_cloud_job_backend_id.py"

# Wave-0 guard: the migration file lands in Wave 1. Until then every test SKIPS (the file still
# collects cleanly). Once 029 exists the guard passes and the assertions run.
_requires_029 = pytest.mark.skipif(
    not _MIGRATION_PATH.exists(),
    reason="migration 029_add_cloud_job_backend_id.py not yet created (lands in Wave 1)",
)


def _load_migration_029() -> object:
    """Load the 029 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_029", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_BACKEND_ID_SQL = "SELECT column_name FROM information_schema.columns WHERE table_name = 'cloud_job' AND column_name = 'backend_id'"
_S3_KEY_NULLABLE_SQL = "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'cloud_job' AND column_name = 's3_key'"
_BACKEND_ID_NULLABLE_SQL = "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'cloud_job' AND column_name = 'backend_id'"


@_requires_029
def test_revision_identifiers_are_bare_numbers() -> None:
    """029 chains off 028 using bare-number strings (no long migration names)."""
    migration_029 = _load_migration_029()
    assert migration_029.revision == "029"  # type: ignore[attr-defined]
    assert migration_029.down_revision == "028"  # type: ignore[attr-defined]
    assert migration_029.branch_labels is None  # type: ignore[attr-defined]


@_requires_029
def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/026 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 029 must not reference saq_jobs outside its banner: {offending}"


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


@_requires_029
@pytest.mark.asyncio
async def test_upgrade_029_adds_backend_id_and_nullable_s3_key_then_downgrade_reverses() -> None:
    """029 adds nullable ``backend_id`` (no backfill) + makes ``s3_key`` nullable; downgrade reverses."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "028")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition at 028: backend_id absent, and s3_key is NOT NULL.
        async with engine.connect() as conn:
            before = {row[0] for row in (await conn.execute(text(_BACKEND_ID_SQL))).all()}
            assert before == set(), f"backend_id must not exist before migration 029: {before}"
            s3_nullable_before = (await conn.execute(text(_S3_KEY_NULLABLE_SQL))).scalar_one()
            assert s3_nullable_before == "NO", "s3_key must be NOT NULL at revision 028"

        # A NULL s3_key is rejected at 028 (NOT NULL still in force).
        fid0 = "00000000-0000-0000-0000-000000000000"
        await _seed_file(engine, fid0, "pre")
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, NULL, 'submitted', NOW(), NOW())"
                    ),
                    {"fid": fid0},
                )

        await asyncio.to_thread(upgrade_to, cfg, "029")

        # backend_id now exists and is nullable; s3_key is now nullable (D-08).
        async with engine.connect() as conn:
            after = {row[0] for row in (await conn.execute(text(_BACKEND_ID_SQL))).all()}
            assert after == {"backend_id"}, f"backend_id must exist after 029: {after}"
            backend_nullable = (await conn.execute(text(_BACKEND_ID_NULLABLE_SQL))).scalar_one()
            assert backend_nullable == "YES", "backend_id must be nullable (D-06: config-derived, no backfill)"
            s3_nullable_after = (await conn.execute(text(_S3_KEY_NULLABLE_SQL))).scalar_one()
            assert s3_nullable_after == "YES", "s3_key must be nullable after 029 (D-08: compute has no S3 object)"

        # A compute-shaped row (s3_key NULL, backend_id stamped) now inserts cleanly -- proving both
        # the D-08 nullability and that backend_id accepts a config-derived id going forward.
        fid1 = "11111111-1111-1111-1111-111111111111"
        await _seed_file(engine, fid1, "compute")
        async with engine.begin() as wconn:
            await wconn.execute(
                text(
                    "INSERT INTO cloud_job (id, file_id, s3_key, backend_id, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, NULL, 'compute-a1', 'submitted', NOW(), NOW())"
                ),
                {"fid": fid1},
            )
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT backend_id, s3_key FROM cloud_job WHERE file_id = :fid"), {"fid": fid1})).one()
            assert row[0] == "compute-a1"
            assert row[1] is None  # s3_key NULL for a compute burst

        # Clear the NULL-s3_key row -- the downgrade re-imposes NOT NULL and would reject it.
        async with engine.begin() as wconn:
            await wconn.execute(text("DELETE FROM cloud_job"))

        # Downgrade to 028: backend_id gone, s3_key NOT NULL again (a NULL s3_key is rejected).
        await asyncio.to_thread(downgrade_to, cfg, "028")
        async with engine.connect() as conn:
            post = {row[0] for row in (await conn.execute(text(_BACKEND_ID_SQL))).all()}
            assert post == set(), f"downgrade to 028 must drop backend_id: {post}"
            s3_nullable_post = (await conn.execute(text(_S3_KEY_NULLABLE_SQL))).scalar_one()
            assert s3_nullable_post == "NO", "s3_key must be NOT NULL again after downgrade to 028"

        fid_post = "44444444-4444-4444-4444-444444444444"
        await _seed_file(engine, fid_post, "post")
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, NULL, 'submitted', NOW(), NOW())"
                    ),
                    {"fid": fid_post},
                )
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
