"""Tests for migration 030: add ``cloud_job.staging_bucket`` (Phase 70, D-01/D-02, MKUE-04).

Mirrors ``test_migration_029_backend_id.py``: static revision-id assertions run WITHOUT a DB so the
additive-only / bare-number contract holds even where Postgres is absent; the integration body
upgrades 029 -> 030, proves ``staging_bucket`` exists (nullable, no backfill), then downgrades to 029
and proves the column is gone.

GUARDED SCAFFOLD. Migration ``030_add_cloud_job_staging_bucket.py`` lands in Wave 1; until then a
file-exists ``skipif`` guards every test so this file COLLECTS cleanly in Wave 0 and lights up
automatically once the migration appears.

D-01/D-06: ``staging_bucket`` records which ``BucketConfig.id`` staged the current object. It is a
plain nullable ``String`` -- NO CHECK/enum change and NO backfill (029's "the a1/k8s paths were never
deployed live so there are ~zero rows to migrate" rationale applies verbatim). It is added going
forward; the migration cannot know a per-file bucket choice.
D-02: ``unique(file_id)`` is untouched -- cloud_job stays one-row-per-file.

CRITICAL: migration 030 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/026/029 banner).
A grep-style assertion enforces this.

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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "030_add_cloud_job_staging_bucket.py"

# Wave-0 guard: the migration file lands in Wave 1. Until then every test SKIPS (the file still
# collects cleanly). Once 030 exists the guard passes and the assertions run.
_requires_030 = pytest.mark.skipif(
    not _MIGRATION_PATH.exists(),
    reason="migration 030_add_cloud_job_staging_bucket.py not yet created (lands in Wave 1)",
)


def _load_migration_030() -> object:
    """Load the 030 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_030", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_STAGING_BUCKET_SQL = "SELECT column_name FROM information_schema.columns WHERE table_name = 'cloud_job' AND column_name = 'staging_bucket'"
_STAGING_BUCKET_NULLABLE_SQL = "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'cloud_job' AND column_name = 'staging_bucket'"


@_requires_030
def test_revision_identifiers_are_bare_numbers() -> None:
    """030 chains off 029 using bare-number strings (no long migration names)."""
    migration_030 = _load_migration_030()
    assert migration_030.revision == "030"  # type: ignore[attr-defined]
    assert migration_030.down_revision == "029"  # type: ignore[attr-defined]
    assert migration_030.branch_labels is None  # type: ignore[attr-defined]


@_requires_030
def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/026/029 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 030 must not reference saq_jobs outside its banner: {offending}"


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


@_requires_030
@pytest.mark.asyncio
async def test_upgrade_030_adds_nullable_staging_bucket_then_downgrade_reverses() -> None:
    """030 adds nullable ``staging_bucket`` (no backfill); downgrade drops it; unique(file_id) untouched."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "029")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition at 029: staging_bucket absent.
        async with engine.connect() as conn:
            before = {row[0] for row in (await conn.execute(text(_STAGING_BUCKET_SQL))).all()}
            assert before == set(), f"staging_bucket must not exist before migration 030: {before}"

        await asyncio.to_thread(upgrade_to, cfg, "030")

        # staging_bucket now exists and is nullable.
        async with engine.connect() as conn:
            after = {row[0] for row in (await conn.execute(text(_STAGING_BUCKET_SQL))).all()}
            assert after == {"staging_bucket"}, f"staging_bucket must exist after 030: {after}"
            staging_nullable = (await conn.execute(text(_STAGING_BUCKET_NULLABLE_SQL))).scalar_one()
            assert staging_nullable == "YES", "staging_bucket must be nullable (D-01/D-06: recorded going forward, no backfill)"

        # A row stamping staging_bucket inserts cleanly, and unique(file_id) is preserved (D-02): a
        # second cloud_job for the same file_id is rejected.
        fid1 = "11111111-1111-1111-1111-111111111111"
        await _seed_file(engine, fid1, "staged")
        async with engine.begin() as wconn:
            await wconn.execute(
                text(
                    "INSERT INTO cloud_job (id, file_id, s3_key, staging_bucket, status, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :fid, 'phaze-staging/x', 'staging-a', 'submitted', NOW(), NOW())"
                ),
                {"fid": fid1},
            )
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT staging_bucket FROM cloud_job WHERE file_id = :fid"), {"fid": fid1})).one()
            assert row[0] == "staging-a"

        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, staging_bucket, status, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :fid, 'phaze-staging/y', 'staging-b', 'submitted', NOW(), NOW())"
                    ),
                    {"fid": fid1},
                )

        # Clear rows so the downgrade drop_column has nothing to trip on.
        async with engine.begin() as wconn:
            await wconn.execute(text("DELETE FROM cloud_job"))

        # Downgrade to 029: staging_bucket gone.
        await asyncio.to_thread(downgrade_to, cfg, "029")
        async with engine.connect() as conn:
            post = {row[0] for row in (await conn.execute(text(_STAGING_BUCKET_SQL))).all()}
            assert post == set(), f"downgrade to 029 must drop staging_bucket: {post}"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
