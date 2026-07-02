"""Tests for migration 026: add the Kube lifecycle columns to ``cloud_job`` (Phase 54, D-09).

Mirrors ``test_migration_025_cloud_job.py``: static revision-id assertions run WITHOUT a DB so
the additive-only / bare-number contract holds even where Postgres is absent; the integration
body upgrades to 025 then 026, proves the 3 new columns exist and the widened status CHECK
accepts the new SUBMITTED/RUNNING/SUCCEEDED members (and still rejects an unknown value), then
downgrades to 025 and proves the columns are gone and the CHECK rejects ``'submitted'`` again.

CRITICAL: migration 026 must NEVER reference ``saq_jobs`` (SAQ owns that table). A grep-style
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "026_add_cloud_job_kube_columns.py"


def _load_migration_026() -> object:
    """Load the 026 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_026", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'cloud_job' AND column_name IN ('kueue_workload', 'attempts', 'inadmissible')"
)
_KUBE_COLUMNS = {"kueue_workload", "attempts", "inadmissible"}


def test_revision_identifiers_are_bare_numbers() -> None:
    """026 chains off 025 using bare-number strings (no long migration names)."""
    migration_026 = _load_migration_026()
    assert migration_026.revision == "026"  # type: ignore[attr-defined]
    assert migration_026.down_revision == "025"  # type: ignore[attr-defined]
    assert migration_026.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 026 must not reference saq_jobs outside its banner: {offending}"


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
async def test_upgrade_026_adds_kube_columns_then_downgrade_drops() -> None:
    """026 adds kueue_workload/attempts/inadmissible + widens the status CHECK; downgrade reverses cleanly."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "025")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Pre-condition: the 3 kube columns are absent at revision 025, and 'submitted' is rejected.
        async with engine.connect() as conn:
            before = {row[0] for row in (await conn.execute(text(_COLUMNS_SQL))).all()}
            assert before == set(), f"kube columns must not exist before migration 026: {before}"

        fid0 = "00000000-0000-0000-0000-000000000000"
        await _seed_file(engine, fid0, "pre")
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :fid, :k, 'submitted', NOW(), NOW())"
                    ),
                    {"fid": fid0, "k": "staging/pre"},
                )

        await asyncio.to_thread(upgrade_to, cfg, "026")

        # The 3 columns now exist.
        async with engine.connect() as conn:
            after = {row[0] for row in (await conn.execute(text(_COLUMNS_SQL))).all()}
            assert after == _KUBE_COLUMNS, f"all 3 kube columns must exist after 026: {after}"

        # The widened CHECK accepts the new lifecycle members and stamps the defaults.
        fid1 = "11111111-1111-1111-1111-111111111111"
        await _seed_file(engine, fid1, "ok")
        async with engine.begin() as wconn:
            await wconn.execute(
                text(
                    "INSERT INTO cloud_job (id, file_id, s3_key, status, kueue_workload, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :fid, :k, 'submitted', 'phaze-analyze-ok', NOW(), NOW())"
                ),
                {"fid": fid1, "k": "staging/ok"},
            )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT status, kueue_workload, attempts, inadmissible FROM cloud_job WHERE file_id = :fid"),
                    {"fid": fid1},
                )
            ).one()
            assert row[0] == "submitted"
            assert row[1] == "phaze-analyze-ok"
            assert row[2] == 0  # server_default
            assert row[3] is False  # server_default

        # 'running' and 'succeeded' are also accepted; an unknown value is still rejected.
        for idx, status in enumerate(("running", "succeeded")):
            fid = f"22222222-2222-2222-2222-22222222220{idx}"
            await _seed_file(engine, fid, f"st{idx}")
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :fid, :k, :s, NOW(), NOW())"
                    ),
                    {"fid": fid, "k": f"staging/{status}", "s": status},
                )

        fid_bad = "33333333-3333-3333-3333-333333333333"
        await _seed_file(engine, fid_bad, "bad")
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :fid, :k, 'bogus', NOW(), NOW())"
                    ),
                    {"fid": fid_bad, "k": "staging/bad"},
                )

        # Clear the rows carrying the new lifecycle statuses -- the narrowed CHECK the downgrade
        # recreates would reject them (a real downgrade requires the operator to drain in-flight
        # kube work first; here we just exercise the schema reversal).
        async with engine.begin() as wconn:
            await wconn.execute(text("DELETE FROM cloud_job"))

        # Downgrade to 025: columns gone, CHECK narrowed (so 'submitted' is rejected again).
        await asyncio.to_thread(downgrade_to, cfg, "025")
        async with engine.connect() as conn:
            post = {row[0] for row in (await conn.execute(text(_COLUMNS_SQL))).all()}
            assert post == set(), f"downgrade to 025 must drop all 3 kube columns: {post}"

        fid_post = "44444444-4444-4444-4444-444444444444"
        await _seed_file(engine, fid_post, "post")
        with pytest.raises(IntegrityError):
            async with engine.begin() as wconn:
                await wconn.execute(
                    text(
                        "INSERT INTO cloud_job (id, file_id, s3_key, status, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :fid, :k, 'submitted', NOW(), NOW())"
                    ),
                    {"fid": fid_post, "k": "staging/post"},
                )
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
