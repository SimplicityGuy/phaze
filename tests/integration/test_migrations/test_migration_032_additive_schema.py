"""Tests for migration 032: additive derived-status substrate + backfill (Phase 77, MIG-01/PERF-01).

Mirrors ``test_migration_031_route_control.py`` / ``test_migration_029_backend_id.py``: static
revision-id + ``saq_jobs``-banner assertions run WITHOUT a DB (the additive-only / bare-number
contract holds even where Postgres is absent); the integration body seeds a small corpus at 031,
upgrades 031 -> 032, and proves:

* the ``failed_at``/``error_message`` columns on ``analysis``/``metadata`` + the ``dedup_resolution``
  table + the widened ``status_enum`` CHECK ('awaiting') all exist;
* the analyze-failed backfill is an UPSERT -- every ``state='analysis_failed'`` file (WITH or WITHOUT
  a pre-existing partial ``analysis`` row) ends with ``failed_at`` set (RESEARCH Pitfall 2);
* ``metadata.failed_at`` stays all-NULL (D-03: metadata gets NO backfill);
* the dedup backfill inserts one row per ``duplicate_resolved`` file, deriving ``canonical_file_id``
  deterministically (non-resolved same-sha256 member; NULL if none -- RESEARCH Pitfall 4);
* the cloud sidecar gap-fill inserts ``awaiting``/``uploading``/``uploaded`` rows for
  ``awaiting_cloud``/``pushing``/``pushed`` files missing one, and does NOT duplicate an existing row (D-04/D-06);
* ``files.state`` is byte-unchanged (snapshot before/after -- ROADMAP SC#1 hard invariant);
* the 5 partial indexes exist in ``pg_indexes``;
* ``alembic`` autogenerate against the 032 head produces an EMPTY diff for the 032 objects
  (PERF-01 SC#2 -- the ORM ``__table_args__`` mirror parity);
* the minimal downgrade removes the additive DDL objects (D-09).

CRITICAL: migration 032 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031 banner). A
grep-style assertion enforces this.

Operator pre-condition for the integration body: the database ``phaze_migrations_test`` must exist
(see ``tests/integration/test_migrations/conftest.py``); run via ``just integration-test`` /
``just test-db``.
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "032_add_derived_status_schema.py"

# The 032 schema objects the empty-autogenerate-diff contract (PERF-01 SC#2) is scoped to. Unrelated
# pre-existing ORM<->DB drift (not introduced by this phase) is deliberately NOT in scope.
_O32_TABLES = {"dedup_resolution"}
_O32_INDEXES = {"ix_analysis_completed", "ix_analysis_failed", "ix_metadata_failed", "ix_cloud_job_awaiting", "ix_fprint_success"}
_O32_COLUMNS = {("analysis", "failed_at"), ("analysis", "error_message"), ("metadata", "failed_at"), ("metadata", "error_message")}

# Fixed seed UUIDs (readable last nibble = role).
_FB = "00000000-0000-0000-0000-0000000000b0"  # canonical target (state=analyzed, shares dup hash)
_FA = "00000000-0000-0000-0000-0000000000a0"  # duplicate_resolved, canonical derivable -> _FB
_FC = "00000000-0000-0000-0000-0000000000c0"  # duplicate_resolved, lonely hash -> canonical NULL
_FD = "00000000-0000-0000-0000-0000000000d0"  # analysis_failed, NO pre-existing analysis row (INSERT branch)
_FE = "00000000-0000-0000-0000-0000000000e0"  # analysis_failed, WITH partial analysis row (DO UPDATE branch)
_FF = "00000000-0000-0000-0000-0000000000f0"  # awaiting_cloud, no cloud_job -> status='awaiting'
_FG = "00000000-0000-0000-0000-000000000011"  # pushing, no cloud_job -> status='uploading'
_FH = "00000000-0000-0000-0000-000000000022"  # pushed, no cloud_job -> status='uploaded'
_FI = "00000000-0000-0000-0000-000000000033"  # pushing, WITH existing cloud_job -> gap-fill DO NOTHING

_DUP_HASH = "dup-hash-shared-0001"
_LONELY_HASH = "lonely-hash-0002"

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, :st, NOW(), NOW())"
)


def _load_migration_032() -> object:
    """Load the 032 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_032", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revision_identifiers_are_bare_numbers() -> None:
    """032 chains off 031 using bare-number strings (no long migration names)."""
    migration_032 = _load_migration_032()
    assert migration_032.revision == "032"  # type: ignore[attr-defined]
    assert migration_032.down_revision == "031"  # type: ignore[attr-defined]
    assert migration_032.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/031 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 032 must not reference saq_jobs outside its banner: {offending}"


async def _seed_file(engine, fid: str, path: str, state: str, sha: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row in the given ``state`` (FK to the 012-seeded legacy agent)."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_FILE_SQL), {"id": fid, "h": sha, "p": path, "n": path.rsplit("/", 1)[-1], "st": state})


async def _seed_corpus(engine) -> None:  # type: ignore[no-untyped-def]
    """Seed one file in each relevant legacy ``files.state`` + the pre-existing analysis/metadata/cloud rows."""
    await _seed_file(engine, _FB, "/music/canonical.flac", "analyzed", _DUP_HASH)
    await _seed_file(engine, _FA, "/music/dup_a.flac", "duplicate_resolved", _DUP_HASH)
    await _seed_file(engine, _FC, "/music/dup_c.flac", "duplicate_resolved", _LONELY_HASH)
    await _seed_file(engine, _FD, "/music/failed_norow.flac", "analysis_failed", "hash-d")
    await _seed_file(engine, _FE, "/music/failed_partial.flac", "analysis_failed", "hash-e")
    await _seed_file(engine, _FF, "/music/awaiting.flac", "awaiting_cloud", "hash-f")
    await _seed_file(engine, _FG, "/music/pushing.flac", "pushing", "hash-g")
    await _seed_file(engine, _FH, "/music/pushed.flac", "pushed", "hash-h")
    await _seed_file(engine, _FI, "/music/pushing_existing.flac", "pushing", "hash-i")
    async with engine.begin() as conn:
        # _FE carries a partial analysis row (analysis START upsert precedent) -> exercises DO UPDATE.
        await conn.execute(
            text("INSERT INTO analysis (id, file_id, created_at, updated_at) VALUES (gen_random_uuid(), :fid, NOW(), NOW())"), {"fid": _FE}
        )
        # _FB carries a metadata row -> proves metadata.failed_at stays NULL (D-03, no backfill).
        await conn.execute(
            text("INSERT INTO metadata (id, file_id, created_at, updated_at) VALUES (gen_random_uuid(), :fid, NOW(), NOW())"), {"fid": _FB}
        )
        # _FI already has a cloud_job row -> the pushing gap-fill must DO NOTHING (no duplicate).
        await conn.execute(
            text("INSERT INTO cloud_job (id, file_id, status, created_at, updated_at) VALUES (gen_random_uuid(), :fid, 'uploading', NOW(), NOW())"),
            {"fid": _FI},
        )


def _diffs_touching_032(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would create/drop a 032 object (PERF-01 empty-diff scope)."""
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
        if (op_name in ("add_table", "remove_table") and getattr(diff[1], "name", None) in _O32_TABLES) or (
            op_name in ("add_index", "remove_index") and getattr(diff[1], "name", None) in _O32_INDEXES
        ):
            offenders.append((op_name, diff[1].name))
        elif op_name in ("add_column", "remove_column") and (diff[2], diff[3].name) in _O32_COLUMNS:
            offenders.append((op_name, f"{diff[2]}.{diff[3].name}"))
    return offenders


@pytest.mark.asyncio
async def test_upgrade_032_creates_backfills_and_autogenerate_is_empty_then_downgrade_reverses() -> None:
    """032 creates the additive objects, backfills from ``files.state`` (unchanged), diffs empty, downgrade reverses."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "031")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_corpus(engine)

        # Snapshot files.state (the byte-unchanged invariant target) BEFORE the migration.
        async with engine.connect() as conn:
            before_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}

        await asyncio.to_thread(upgrade_to, cfg, "032")

        async with engine.connect() as conn:
            # (a) new columns exist on analysis + metadata.
            cols = {
                (r[0], r[1])
                for r in (
                    await conn.execute(
                        text(
                            "SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_name IN ('analysis','metadata') AND column_name IN ('failed_at','error_message')"
                        )
                    )
                ).all()
            }
            assert cols == {("analysis", "failed_at"), ("analysis", "error_message"), ("metadata", "failed_at"), ("metadata", "error_message")}, cols

            # (b) dedup_resolution table exists.
            dedup_exists = (await conn.execute(text("SELECT to_regclass('public.dedup_resolution')"))).scalar_one()
            assert dedup_exists is not None, "dedup_resolution table must exist after 032"

            # (c/d) analyze-failed UPSERT: every analysis_failed file (with OR without a prior row) has failed_at set.
            failed_files = (await conn.execute(text("SELECT count(*) FROM files WHERE state = 'analysis_failed'"))).scalar_one()
            failed_markers = (await conn.execute(text("SELECT count(*) FROM analysis WHERE failed_at IS NOT NULL"))).scalar_one()
            assert failed_markers == failed_files == 2, (failed_markers, failed_files)
            # both the no-row (_FD) and the partial-row (_FE) file carry a marker now.
            marked = {str(r[0]) for r in (await conn.execute(text("SELECT file_id FROM analysis WHERE failed_at IS NOT NULL"))).all()}
            assert marked == {_FD, _FE}, marked

            # (e) metadata.failed_at stays all-NULL (D-03: no backfill).
            meta_marked = (await conn.execute(text("SELECT count(*) FROM metadata WHERE failed_at IS NOT NULL"))).scalar_one()
            assert meta_marked == 0, "metadata.failed_at must have NO backfill (D-03)"

            # (f/g) dedup backfill: one row per duplicate_resolved file; canonical derived deterministically.
            resolved_files = (await conn.execute(text("SELECT count(*) FROM files WHERE state = 'duplicate_resolved'"))).scalar_one()
            dedup_rows = (await conn.execute(text("SELECT count(*) FROM dedup_resolution"))).scalar_one()
            assert dedup_rows == resolved_files == 2, (dedup_rows, resolved_files)
            canon_a = (await conn.execute(text("SELECT canonical_file_id FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FA})).scalar_one()
            assert str(canon_a) == _FB, f"_FA canonical must derive to the non-resolved same-hash member _FB: {canon_a}"
            canon_c = (await conn.execute(text("SELECT canonical_file_id FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FC})).scalar_one()
            assert canon_c is None, f"_FC has no non-resolved same-hash member -> canonical NULL: {canon_c}"

            # (h) cloud sidecar gap-fill: awaiting/uploading/uploaded present; existing row NOT duplicated.
            status_ff = (await conn.execute(text("SELECT status FROM cloud_job WHERE file_id = :fid"), {"fid": _FF})).scalar_one()
            status_fg = (await conn.execute(text("SELECT status FROM cloud_job WHERE file_id = :fid"), {"fid": _FG})).scalar_one()
            status_fh = (await conn.execute(text("SELECT status FROM cloud_job WHERE file_id = :fid"), {"fid": _FH})).scalar_one()
            assert (status_ff, status_fg, status_fh) == ("awaiting", "uploading", "uploaded"), (status_ff, status_fg, status_fh)
            fi_rows = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE file_id = :fid"), {"fid": _FI})).scalar_one()
            assert fi_rows == 1, "pushing gap-fill must DO NOTHING for a file that already has a cloud_job row (D-06)"
            awaiting_count = (await conn.execute(text("SELECT count(*) FROM cloud_job WHERE status = 'awaiting'"))).scalar_one()
            assert awaiting_count == 1, awaiting_count

            # (i) files.state byte-unchanged.
            after_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}
            assert after_state == before_state, "files.state must be byte-unchanged after 032"

            # (j) all 5 partial indexes exist.
            idx = {
                r[0]
                for r in (
                    await conn.execute(text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"), {"names": list(_O32_INDEXES)})
                ).all()
            }
            assert idx == _O32_INDEXES, f"missing partial indexes: {_O32_INDEXES - idx}"

        # (k) PERF-01 SC#2: autogenerate against the 032 head yields an EMPTY diff for the 032 objects.
        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_032)
        assert offenders == [], f"autogenerate churn on 032 objects breaks the empty-diff contract (PERF-01): {offenders}"

        # Clear the backfilled cloud_job rows before downgrading: the 032 downgrade restores the
        # 6-member CHECK (rejecting the 'awaiting' rows) and the eventual teardown to base walks
        # migration 029's downgrade which re-imposes ``s3_key NOT NULL`` (rejecting the NULL-s3_key
        # sidecar rows). Deleting them all is the 029-precedent cleanup.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM cloud_job"))

        # (l) minimal downgrade removes the additive DDL objects (D-09).
        await asyncio.to_thread(downgrade_to, cfg, "031")
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT to_regclass('public.dedup_resolution')"))).scalar_one() is None, (
                "downgrade must drop dedup_resolution"
            )
            post_cols = {
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_name = 'analysis' AND column_name IN ('failed_at','error_message')"
                        )
                    )
                ).all()
            }
            assert post_cols == set(), f"downgrade must drop the analysis failure-marker columns: {post_cols}"
            post_idx = {
                r[0]
                for r in (
                    await conn.execute(text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"), {"names": list(_O32_INDEXES)})
                ).all()
            }
            assert post_idx == set(), f"downgrade must drop the partial indexes: {post_idx}"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
