"""Tests for migration 035: bidirectional reconcile of dedup_resolution vs files.state (Phase 84, SIDECAR-02, D-04).

Mirrors ``test_migration_034_backfill_cloud_awaiting.py``: static revision-id + ``saq_jobs``-banner +
static-SQL assertions run WITHOUT a DB; the integration body seeds a small corpus at ``034``, upgrades
``034 -> 035``, and proves the D-04 BOTH-DIRECTION reconcile contract:

* a ``state='duplicate_resolved'`` file with NO marker gains exactly one ``dedup_resolution`` row whose
  ``canonical_file_id`` is derived by the subquery (the missing-marker insert half -- ``032``'s
  ``_BACKFILL_DEDUP`` re-run verbatim);
* an ORPHANED marker (marker present, ``state <> 'duplicate_resolved'``) is DELETED (the new delete half);
* a control non-resolved file with NO marker stays row-less;
* a resolved file that already carried a marker is UNCHANGED (``ON CONFLICT (file_id) DO NOTHING`` +
  ``uq_dedup_resolution_file_id`` -- no clobber, no duplicate);
* re-running the insert half is INERT -- no duplicate rows, the marker count is stable (idempotency);
* ``alembic`` autogenerate against the ``035`` head produces an EMPTY diff (``035`` touches NO
  ORM-mapped schema -- the ``dedup_resolution`` table + ``uq_dedup_resolution_file_id`` shipped in ``032``);
* ``files.state`` is byte-unchanged (``035`` is READ-ONLY on ``files.state``);
* the documented NO-OP downgrade runs and leaves the reconciled markers UNCHANGED.

CRITICAL: migration 035 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).

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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "035_reconcile_dedup_resolution.py"

# 035 touches NO ORM-mapped schema, so the empty-diff assertion is scoped to the dedup_resolution objects
# the reconcile concerns (the table + unique FK that shipped in 032). Unrelated pre-existing ORM<->DB
# drift is deliberately NOT in scope.
_O35_TABLES: set[str] = set()
_O35_INDEXES: set[str] = set()
_O35_COLUMNS: set[tuple[str, str]] = set()

# Fixed seed UUIDs (readable last nibble = role).
_FA = "00000000-0000-0000-0000-0000000000a0"  # duplicate_resolved, NO marker -> gains one (canonical derived)
_FCAN = "00000000-0000-0000-0000-0000000000a1"  # analyzed, same sha256 as _FA -> the derived canonical target
_FB = "00000000-0000-0000-0000-0000000000b0"  # analyzed, WITH orphaned marker -> marker DELETED
_FC = "00000000-0000-0000-0000-0000000000c0"  # analyzed (control, not resolved), NO marker -> stays row-less
_FD = "00000000-0000-0000-0000-0000000000d0"  # duplicate_resolved, WITH pre-existing marker -> DO NOTHING
_MARKER_FB = "00000000-0000-0000-0000-0000000000b1"  # _FB's orphaned marker (must be deleted)
_MARKER_FD = "00000000-0000-0000-0000-0000000000d1"  # _FD's pre-existing marker (must survive unchanged)

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, :st, NOW(), NOW())"
)


def _load_migration_035() -> object:
    """Load the 035 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_035", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revision_identifiers_are_bare_numbers() -> None:
    """035 chains off 034 using bare-number strings (no long migration names)."""
    migration_035 = _load_migration_035()
    assert migration_035.revision == "035"  # type: ignore[attr-defined]
    assert migration_035.down_revision == "034"  # type: ignore[attr-defined]
    assert migration_035.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/031/032 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 035 must not reference saq_jobs outside its banner: {offending}"


def test_backfill_sql_is_static_and_parameter_free() -> None:
    """035's reconcile SQL is static strings -- no ``%``/``.format``/f-string interpolation (T-84-01-01)."""
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "ON CONFLICT (file_id) DO NOTHING" in body
    assert "WHERE f.state = 'duplicate_resolved'" in body  # insert half
    assert "f.state <> 'duplicate_resolved'" in body  # delete half
    # No interpolation surface reaches either statement.
    assert ".format(" not in body
    assert "f'''" not in body and 'f"""' not in body


async def _seed_file(engine, fid: str, path: str, state: str, sha: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row in the given ``state`` (FK to the 012-seeded legacy agent)."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_FILE_SQL), {"id": fid, "h": sha, "p": path, "n": path.rsplit("/", 1)[-1], "st": state})


async def _seed_marker(engine, mid: str, fid: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a bare ``dedup_resolution`` marker (canonical NULL) with a fixed id, defaults for timestamps."""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO dedup_resolution (id, file_id) VALUES (:mid, :fid)"),
            {"mid": mid, "fid": fid},
        )


async def _seed_corpus(engine) -> None:  # type: ignore[no-untyped-def]
    """Seed the D-04 both-direction reconcile corpus."""
    # Direction 1 (insert): resolved, no marker; plus a same-sha256 non-resolved canonical target.
    await _seed_file(engine, _FA, "/music/resolved_no_marker.flac", "duplicate_resolved", "hash-a")
    await _seed_file(engine, _FCAN, "/music/canonical_keeper.flac", "analyzed", "hash-a")
    # Direction 2 (delete): non-resolved file carrying an ORPHANED marker.
    await _seed_file(engine, _FB, "/music/orphan_marker.flac", "analyzed", "hash-b")
    # Control: non-resolved, no marker.
    await _seed_file(engine, _FC, "/music/control_analyzed.flac", "analyzed", "hash-c")
    # Idempotent skip: resolved WITH a pre-existing marker.
    await _seed_file(engine, _FD, "/music/resolved_existing.flac", "duplicate_resolved", "hash-d")
    await _seed_marker(engine, _MARKER_FB, _FB)  # orphaned -> must be deleted
    await _seed_marker(engine, _MARKER_FD, _FD)  # pre-existing on a resolved file -> DO NOTHING, survives


def _diffs_touching_035(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would create/drop a 035-scoped object (empty-diff scope)."""
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
        if (op_name in ("add_table", "remove_table") and getattr(diff[1], "name", None) in _O35_TABLES) or (
            op_name in ("add_index", "remove_index") and getattr(diff[1], "name", None) in _O35_INDEXES
        ):
            offenders.append((op_name, diff[1].name))
        elif op_name in ("add_column", "remove_column") and (diff[2], diff[3].name) in _O35_COLUMNS:
            offenders.append((op_name, f"{diff[2]}.{diff[3].name}"))
    return offenders


@pytest.mark.asyncio
async def test_upgrade_035_reconciles_both_directions_idempotent_autogenerate_empty_then_downgrade() -> None:
    """035 inserts the missing marker + deletes the orphaned one, is idempotent, diffs empty, downgrade is a no-op."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "034")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_corpus(engine)

        # Snapshot files.state (035 is READ-ONLY on it) BEFORE the migration.
        async with engine.connect() as conn:
            before_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}

        await asyncio.to_thread(upgrade_to, cfg, "035")

        async with engine.connect() as conn:
            # (a) INSERT half: _FA (resolved, no marker) now has exactly one marker; canonical = _FCAN
            #     (the non-resolved same-sha256 member picked by the subquery).
            rows_fa = (await conn.execute(text("SELECT count(*) FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FA})).scalar_one()
            assert rows_fa == 1, "resolved-no-marker file must gain exactly one marker"
            canonical_fa = (
                await conn.execute(text("SELECT canonical_file_id FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FA})
            ).scalar_one()
            assert str(canonical_fa) == _FCAN, f"canonical_file_id must be the derived non-resolved same-sha256 keeper: {canonical_fa}"

            # (b) DELETE half: _FB's orphaned marker (state<>'duplicate_resolved') is gone.
            rows_fb = (await conn.execute(text("SELECT count(*) FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FB})).scalar_one()
            assert rows_fb == 0, "orphaned marker on a non-resolved file must be deleted"

            # (c) the control non-resolved file has NO marker (never had one; insert skips, delete no-op).
            rows_fc = (await conn.execute(text("SELECT count(*) FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FC})).scalar_one()
            assert rows_fc == 0, "control (non-resolved) file must NOT gain a marker"

            # (d) _FCAN (canonical target, non-resolved) stays row-less itself.
            rows_fcan = (await conn.execute(text("SELECT count(*) FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FCAN})).scalar_one()
            assert rows_fcan == 0, "the canonical keeper (non-resolved) must NOT gain a marker"

            # (e) the pre-existing marker on the resolved _FD is UNCHANGED (idempotent skip): exactly one
            #     row, same id (DO NOTHING did not clobber; the resolved state protects it from the delete).
            rows_fd = (await conn.execute(text("SELECT count(*) FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FD})).scalar_one()
            id_fd = (await conn.execute(text("SELECT id FROM dedup_resolution WHERE file_id = :fid"), {"fid": _FD})).scalar_one()
            assert rows_fd == 1, "ON CONFLICT DO NOTHING must not duplicate the pre-existing marker"
            assert str(id_fd) == _MARKER_FD, f"pre-existing marker must survive unchanged: {id_fd}"

            # (f) total markers == 2 (_FA inserted + _FD kept; _FB deleted).
            total = (await conn.execute(text("SELECT count(*) FROM dedup_resolution"))).scalar_one()
            assert total == 2, total

            # (g) files.state byte-unchanged (035 is READ-ONLY on files.state).
            after_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}
            assert after_state == before_state, "files.state must be byte-unchanged after 035"

        # (h) IDEMPOTENCY: re-execute the insert half -- ON CONFLICT (file_id) DO NOTHING +
        #     uq_dedup_resolution_file_id make it inert; no duplicate rows, marker count stable.
        migration_035 = _load_migration_035()
        async with engine.begin() as conn:
            await conn.execute(text(migration_035._BACKFILL_DEDUP))  # type: ignore[attr-defined]
        async with engine.connect() as conn:
            total_after_rerun = (await conn.execute(text("SELECT count(*) FROM dedup_resolution"))).scalar_one()
            assert total_after_rerun == 2, total_after_rerun
            per_file = (await conn.execute(text("SELECT file_id, count(*) FROM dedup_resolution GROUP BY file_id HAVING count(*) > 1"))).all()
            assert per_file == [], f"uq_dedup_resolution_file_id + ON CONFLICT must keep one marker per file: {per_file}"

        # (i) EMPTY autogenerate diff after 035 (no ORM-mapped schema drift -- 035 is data-only).
        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_035)
        assert offenders == [], f"035 must not introduce ORM<->DB schema drift (it is data-only): {offenders}"

        # (j) downgrade is a documented NO-OP: assert it runs and the reconciled markers are UNCHANGED.
        await asyncio.to_thread(downgrade_to, cfg, "034")
        async with engine.connect() as conn:
            post_total = (await conn.execute(text("SELECT count(*) FROM dedup_resolution"))).scalar_one()
            assert post_total == 2, "no-op downgrade must leave the reconciled markers unchanged"
        # Clear the marker rows before the teardown walks 032's downgrade (which drops the table).
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM dedup_resolution"))
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
