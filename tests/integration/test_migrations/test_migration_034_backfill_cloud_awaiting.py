"""Tests for migration 034: repair backfill of the missing awaiting cloud_job rows (Phase 83, SIDECAR-01, D-04).

Mirrors ``test_migration_032_additive_schema.py``: static revision-id + ``saq_jobs``-banner assertions
run WITHOUT a DB; the integration body seeds a small corpus at ``033``, upgrades ``033 -> 034``, and
proves the D-04 repair contract:

* every seeded ``state='awaiting_cloud'`` file that had NO ``cloud_job`` row now carries exactly one
  ``cloud_job(status='awaiting')`` row (the go-forward-writer gap ``034`` repairs);
* a control file NOT in ``AWAITING_CLOUD`` gets NO ``cloud_job`` row (the ``WHERE state='awaiting_cloud'``
  filter);
* a file that already carried a ``cloud_job`` row at another status is UNCHANGED
  (``ON CONFLICT (file_id) DO NOTHING`` + ``uq_cloud_job_file_id`` -- no clobber, no duplicate);
* re-running the backfill is INERT -- no duplicate rows, the ``awaiting`` count is stable (idempotency);
* ``alembic`` autogenerate against the ``034`` head produces an EMPTY diff (``034`` touches NO
  ORM-mapped schema -- the ``'awaiting'`` CHECK value and ``ix_cloud_job_awaiting`` shipped in ``032``);
* ``files.state`` is byte-unchanged (``034`` is READ-ONLY on ``files.state``);
* the documented-lossy downgrade runs and removes the ``awaiting`` rows.

CRITICAL: migration 034 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).

FOOTGUN: this test targets the migrations DB on port **5433** (``just test-db`` provisioning), NOT the
5432 default baked into ``conftest.MIGRATIONS_TEST_DATABASE_URL``. Run it via::

    MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test" \\
    just test-bucket integration

``just test-bucket`` does NOT export ``MIGRATIONS_TEST_DATABASE_URL`` -- export it explicitly or the
migration harness silently talks to the wrong (5432) DB and the test fails like an infra flake.
"""

import asyncio
import importlib.util
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import phaze.models  # noqa: F401  -- registers every table on Base.metadata for the autogenerate diff
from phaze.models.base import Base
from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "034_backfill_cloud_awaiting.py"

# 034 touches NO ORM-mapped schema, so the empty-diff assertion is scoped to the cloud_job objects the
# repair concerns (the CHECK value + partial index that shipped in 032). Unrelated pre-existing
# ORM<->DB drift is deliberately NOT in scope.
_O34_TABLES: set[str] = set()
_O34_INDEXES = {"ix_cloud_job_awaiting"}
_O34_COLUMNS: set[tuple[str, str]] = set()

# Fixed seed UUIDs (readable last nibble = role).
_FA = "00000000-0000-0000-0000-0000000000a0"  # awaiting_cloud, NO cloud_job row -> gains status='awaiting'
_FB = "00000000-0000-0000-0000-0000000000b0"  # awaiting_cloud, NO cloud_job row -> gains status='awaiting'
_FC = "00000000-0000-0000-0000-0000000000c0"  # analyzed (control, NOT awaiting_cloud) -> stays row-less
_FD = "00000000-0000-0000-0000-0000000000d0"  # awaiting_cloud, WITH pre-existing cloud_job -> DO NOTHING

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, :st, NOW(), NOW())"
)


def _load_migration_034() -> object:
    """Load the 034 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_034", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revision_identifiers_are_bare_numbers() -> None:
    """034 chains off 033 using bare-number strings (no long migration names)."""
    migration_034 = _load_migration_034()
    assert migration_034.revision == "034"  # type: ignore[attr-defined]
    assert migration_034.down_revision == "033"  # type: ignore[attr-defined]
    assert migration_034.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/031/032 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 034 must not reference saq_jobs outside its banner: {offending}"


def test_backfill_sql_is_static_and_parameter_free() -> None:
    """034's upgrade SQL is a single static string -- no ``%``/``.format``/f-string interpolation (T-83-03)."""
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ON CONFLICT (file_id) DO NOTHING" in body
    assert "WHERE f.state = 'awaiting_cloud'" in body
    # No interpolation surface reaches the backfill statement.
    assert ".format(" not in body
    assert "f'''" not in body and 'f"""' not in body


async def _seed_file(engine, fid: str, path: str, state: str, sha: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row in the given ``state`` (FK to the 012-seeded legacy agent)."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_FILE_SQL), {"id": fid, "h": sha, "p": path, "n": path.rsplit("/", 1)[-1], "st": state})


async def _seed_corpus(engine) -> None:  # type: ignore[no-untyped-def]
    """Seed the D-04 repair corpus: two held row-less awaiting files, one control, one pre-existing row."""
    await _seed_file(engine, _FA, "/music/held_a.flac", "awaiting_cloud", "hash-a")
    await _seed_file(engine, _FB, "/music/held_b.flac", "awaiting_cloud", "hash-b")
    await _seed_file(engine, _FC, "/music/analyzed_control.flac", "analyzed", "hash-c")
    await _seed_file(engine, _FD, "/music/held_existing.flac", "awaiting_cloud", "hash-d")
    async with engine.begin() as conn:
        # _FD already carries a cloud_job row at a NON-awaiting status -> the backfill must DO NOTHING
        # (ON CONFLICT (file_id) DO NOTHING) and leave the status UNCHANGED (no clobber to 'awaiting').
        await conn.execute(
            text("INSERT INTO cloud_job (id, file_id, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, 'submitted', NOW(), NOW())"),
            {"fid": _FD},
        )


def _diffs_touching_034(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would create/drop a 034-scoped object (empty-diff scope)."""
    ctx = MigrationContext.configure(connection=sync_conn, opts={"compare_type": True})
    flat: list = []

    def _flatten(items: list) -> None:
        for item in items:
            if isinstance(item, list):
                _flatten(item)
            else:
                flat.append(item)

    _flatten(compare_metadata(ctx, Base.metadata))

    offenders: list[tuple[str, str]] = []
    for diff in flat:
        op_name = diff[0]
        if (op_name in ("add_table", "remove_table") and getattr(diff[1], "name", None) in _O34_TABLES) or (
            op_name in ("add_index", "remove_index") and getattr(diff[1], "name", None) in _O34_INDEXES
        ):
            offenders.append((op_name, diff[1].name))
        elif op_name in ("add_column", "remove_column") and (diff[2], diff[3].name) in _O34_COLUMNS:
            offenders.append((op_name, f"{diff[2]}.{diff[3].name}"))
    return offenders


@pytest.mark.asyncio
async def test_upgrade_034_repairs_awaiting_rows_idempotently_autogenerate_empty_then_downgrade() -> None:
    """034 backfills a missing ``awaiting`` row per held file, is idempotent, diffs empty, downgrade removes them."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "033")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_corpus(engine)

        # Snapshot files.state (034 is READ-ONLY on it) BEFORE the migration.
        async with engine.connect() as conn:
            before_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}

        await asyncio.to_thread(upgrade_to, cfg, "034")

        async with engine.connect() as conn:
            # (a) every seeded awaiting_cloud file now has exactly one cloud_job(status='awaiting') row --
            #     EXCEPT _FD, which kept its pre-existing 'submitted' row (DO NOTHING).
            status_fa = (await conn.execute(text("SELECT status FROM cloud_job WHERE file_id = :fid"), {"fid": _FA})).scalar_one()
            status_fb = (await conn.execute(text("SELECT status FROM cloud_job WHERE file_id = :fid"), {"fid": _FB})).scalar_one()
            assert (status_fa, status_fb) == ("awaiting", "awaiting"), (status_fa, status_fb)
            rows_fa = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE file_id = :fid"), {"fid": _FA})).scalar_one()
            rows_fb = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE file_id = :fid"), {"fid": _FB})).scalar_one()
            assert rows_fa == rows_fb == 1, (rows_fa, rows_fb)

            # (b) the control non-awaiting file has NO cloud_job row (WHERE state='awaiting_cloud' filter).
            rows_fc = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE file_id = :fid"), {"fid": _FC})).scalar_one()
            assert rows_fc == 0, "control (non-awaiting) file must NOT gain a cloud_job row"

            # (c) the pre-existing row is UNCHANGED (idempotent skip): still exactly one row, still 'submitted'.
            rows_fd = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE file_id = :fid"), {"fid": _FD})).scalar_one()
            status_fd = (await conn.execute(text("SELECT status FROM cloud_job WHERE file_id = :fid"), {"fid": _FD})).scalar_one()
            assert rows_fd == 1, "ON CONFLICT DO NOTHING must not duplicate the pre-existing row"
            assert status_fd == "submitted", f"pre-existing row must NOT be clobbered to 'awaiting': {status_fd}"

            # (d) awaiting-row count == number of held files that had no prior row (_FA, _FB).
            awaiting_count = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE status = 'awaiting'"))).scalar_one()
            assert awaiting_count == 2, awaiting_count

            # (e) files.state byte-unchanged (034 is READ-ONLY on files.state).
            after_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}
            assert after_state == before_state, "files.state must be byte-unchanged after 034"

        # (f) IDEMPOTENCY: re-execute the exact backfill statement -- ON CONFLICT (file_id) DO NOTHING +
        #     uq_cloud_job_file_id make it inert; no duplicate rows, the awaiting count is stable.
        migration_034 = _load_migration_034()
        async with engine.begin() as conn:
            await conn.execute(text(migration_034._BACKFILL_CLOUD_AWAITING))  # type: ignore[attr-defined]
        async with engine.connect() as conn:
            total_after_rerun = (await conn.execute(text("SELECT count(*) FROM cloud_job"))).scalar_one()
            awaiting_after_rerun = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE status = 'awaiting'"))).scalar_one()
            # 3 rows total: _FA(awaiting) + _FB(awaiting) + _FD(submitted). Unchanged by the re-run.
            assert total_after_rerun == 3, total_after_rerun
            assert awaiting_after_rerun == 2, awaiting_after_rerun
            per_file = (await conn.execute(text("SELECT file_id, count(*) FROM cloud_job GROUP BY file_id HAVING count(*) > 1"))).all()
            assert per_file == [], f"uq_cloud_job_file_id + ON CONFLICT must keep one row per file: {per_file}"

        # (g) EMPTY autogenerate diff after 034 (no ORM-mapped schema drift -- 034 is data-only).
        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_034)
        assert offenders == [], f"034 must not introduce ORM<->DB schema drift (it is data-only): {offenders}"

        # (h) downgrade is a documented-lossy DELETE of the awaiting rows; assert it runs and clears them.
        #     Clear the pre-existing 'submitted' row too -- the eventual teardown to base walks 029's
        #     downgrade which re-imposes ``s3_key NOT NULL`` (rejecting the NULL-s3_key sidecar rows).
        await asyncio.to_thread(downgrade_to, cfg, "033")
        async with engine.connect() as conn:
            post_awaiting = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE status = 'awaiting'"))).scalar_one()
            assert post_awaiting == 0, "downgrade must delete every awaiting cloud_job row (documented-lossy)"
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM cloud_job"))
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
