"""Tests for migration 036: data-only backfill of ``analysis.analysis_completed_at`` (Phase 80, READ-03, D-13).

Mirrors ``test_migration_034_backfill_cloud_awaiting.py``: static revision-id + ``saq_jobs``-banner
assertions run WITHOUT a DB; the integration body seeds a small corpus at ``035``, upgrades
``035 -> 036``, and proves the D-13 backfill contract:

* every seeded ``state='analyzed'`` file whose ``analysis`` row had ``analysis_completed_at IS NULL`` and
  ``failed_at IS NULL`` now carries ``analysis_completed_at = updated_at`` (the marker the reenqueue
  cutover reads via ``done_clause(ANALYZE)`` so the ~1001 prod ``analyzed`` rows are NOT re-analyzed);
* a control ``analysis_failed`` file (its ``analysis`` row carries ``failed_at``) is UNCHANGED -- its
  ``analysis_completed_at`` stays NULL and the ``AND failed_at IS NULL`` guard keeps the UPDATE inside
  ``033``'s NAND CheckConstraint (``ck_analysis_analysis_completed_xor_failed``), so the migration does
  NOT abort (VALIDATION SC-4);
* a control NON-``analyzed`` file (``WHERE state='analyzed'`` filter) gets NOTHING;
* re-running the backfill is INERT -- the ``analysis_completed_at IS NULL`` predicate excludes every
  already-stamped row (idempotency);
* ``alembic`` autogenerate against the ``036`` head produces an EMPTY diff (``036`` touches NO
  ORM-mapped schema -- ``analysis_completed_at`` shipped in ``028``);
* ``files.state`` is byte-unchanged (``036`` is READ-ONLY on ``files.state``);
* the documented no-op ``downgrade()`` runs cleanly.

CRITICAL: migration 036 must NEVER reference SAQ's job table (SAQ owns it -- 020/031/032 banner). The
036 source deliberately contains no such literal at all (acceptance criterion #4).

FOOTGUN: this test targets the migrations DB on port **5433** (``just test-db`` provisioning), NOT the
5432 default baked into ``conftest.MIGRATIONS_TEST_DATABASE_URL``. Run it via::

    MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test" \\
    just test-bucket integration

``just test-bucket`` does NOT export ``MIGRATIONS_TEST_DATABASE_URL`` -- export it explicitly or the
migration harness silently talks to the wrong (5432) DB and the test fails like an infra flake.
"""

import asyncio
from datetime import UTC, datetime
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "036_backfill_analysis_completed_at.py"

# 036 touches NO ORM-mapped schema (analysis_completed_at shipped in 028), so the empty-diff scope is
# empty -- 036 concerns no table/index/column that autogenerate would create or drop.
_O36_TABLES: set[str] = set()
_O36_INDEXES: set[str] = set()
_O36_COLUMNS: set[tuple[str, str]] = set()

# A fixed, distinct completion timestamp seeded onto the analyzed rows' updated_at so we can assert the
# backfill copies analysis.updated_at into analysis_completed_at exactly (a raw SQL UPDATE never fires
# the ORM-side onupdate=func.now(), so updated_at stays put across the migration).
_SEED_UPDATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

# Fixed seed UUIDs (readable last nibble = role).
_FA = "00000000-0000-0000-0000-0000000000a0"  # analyzed, analysis row completed=NULL failed=NULL -> stamped
_FB = "00000000-0000-0000-0000-0000000000b0"  # analyzed, analysis row completed=NULL failed=NULL -> stamped
_FC = "00000000-0000-0000-0000-0000000000c0"  # analyzed BUT analysis row failed_at SET -> UNCHANGED (NAND guard TEETH)
_FD = "00000000-0000-0000-0000-0000000000d0"  # discovered (non-analyzed control), analysis row -> UNCHANGED
_FE = "00000000-0000-0000-0000-0000000000e0"  # analyzed, analysis row already completed (go-forward) -> UNCHANGED

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, :st, NOW(), NOW())"
)


def _load_migration_036() -> object:
    """Load the 036 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_036", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revision_identifiers_are_bare_numbers() -> None:
    """036 chains off 035 using bare-number strings (no long migration names)."""
    migration_036 = _load_migration_036()
    assert migration_036.revision == "036"  # type: ignore[attr-defined]
    assert migration_036.down_revision == "035"  # type: ignore[attr-defined]
    assert migration_036.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns its job table -- the migration body must not touch it (020/031/032 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 036 must not reference saq_jobs outside its banner: {offending}"
    # Stronger: 036 carries no such literal at all (acceptance criterion #4).
    assert "saq_jobs" not in _MIGRATION_PATH.read_text(encoding="utf-8")


def test_backfill_sql_is_static_and_parameter_free() -> None:
    """036's upgrade SQL is a single static string -- no ``%``/``.format``/f-string interpolation, NAND-guarded."""
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "f.state = 'analyzed'" in body
    assert "a.analysis_completed_at IS NULL" in body
    # The mandatory NAND guard -- dropping it aborts the migration on analysis_failed rows (VALIDATION SC-4).
    assert "a.failed_at IS NULL" in body
    # No interpolation surface reaches the backfill statement.
    assert ".format(" not in body
    assert "f'''" not in body and 'f"""' not in body


async def _seed_file(engine, fid: str, path: str, state: str, sha: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row in the given ``state`` (FK to the 012-seeded legacy agent)."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_FILE_SQL), {"id": fid, "h": sha, "p": path, "n": path.rsplit("/", 1)[-1], "st": state})


async def _seed_analysis(engine, fid: str, *, failed: bool = False, completed: bool = False) -> None:  # type: ignore[no-untyped-def]
    """Insert an ``analysis`` row for ``fid`` with a fixed ``updated_at`` and optional failed/completed markers."""
    cols = "id, file_id, created_at, updated_at"
    vals = "gen_random_uuid(), :fid, :ts, :ts"
    if failed:
        cols += ", failed_at, error_message"
        vals += ", :ts, 'seed failure'"
    if completed:
        cols += ", analysis_completed_at"
        vals += ", :ts"
    async with engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO analysis ({cols}) VALUES ({vals})"),  # noqa: S608  -- cols/vals are fixed literals, fid is bound
            {"fid": fid, "ts": _SEED_UPDATED_AT},
        )


async def _seed_corpus(engine) -> None:  # type: ignore[no-untyped-def]
    """Seed the D-13 backfill corpus: two stampable analyzed files, one failed control, one non-analyzed, one already-done."""
    await _seed_file(engine, _FA, "/music/analyzed_a.flac", "analyzed", "hash-a")
    await _seed_file(engine, _FB, "/music/analyzed_b.flac", "analyzed", "hash-b")
    # _FC is state='analyzed' (so the WHERE f.state='analyzed' filter DOES match it) but its analysis row
    # carries failed_at -> ONLY the ``AND a.failed_at IS NULL`` NAND guard keeps the UPDATE from stamping it
    # and tripping ck_analysis_analysis_completed_xor_failed. Dropping the guard aborts the migration here.
    await _seed_file(engine, _FC, "/music/analyzed_but_failed.flac", "analyzed", "hash-c")
    await _seed_file(engine, _FD, "/music/discovered_control.flac", "discovered", "hash-d")
    await _seed_file(engine, _FE, "/music/already_done.flac", "analyzed", "hash-e")
    await _seed_analysis(engine, _FA)
    await _seed_analysis(engine, _FB)
    await _seed_analysis(engine, _FC, failed=True)
    await _seed_analysis(engine, _FD)
    await _seed_analysis(engine, _FE, completed=True)


def _diffs_touching_036(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would create/drop a 036-scoped object (empty-diff scope)."""
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
        if (op_name in ("add_table", "remove_table") and getattr(diff[1], "name", None) in _O36_TABLES) or (
            op_name in ("add_index", "remove_index") and getattr(diff[1], "name", None) in _O36_INDEXES
        ):
            offenders.append((op_name, diff[1].name))
        elif op_name in ("add_column", "remove_column") and (diff[2], diff[3].name) in _O36_COLUMNS:
            offenders.append((op_name, f"{diff[2]}.{diff[3].name}"))
    return offenders


@pytest.mark.asyncio
async def test_upgrade_036_backfills_analyzed_rows_idempotently_autogenerate_empty_then_downgrade() -> None:
    """036 stamps analysis_completed_at for analyzed rows, leaves failed/non-analyzed untouched, is idempotent, diffs empty."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "035")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_corpus(engine)

        # Snapshot files.state (036 is READ-ONLY on it) BEFORE the migration.
        async with engine.connect() as conn:
            before_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}

        await asyncio.to_thread(upgrade_to, cfg, "036")

        async with engine.connect() as conn:
            # (1) every stampable analyzed file now carries analysis_completed_at == its seeded updated_at.
            comp_fa = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FA})).scalar_one()
            comp_fb = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FB})).scalar_one()
            assert comp_fa is not None, "analyzed file _FA must be stamped"
            assert comp_fb is not None, "analyzed file _FB must be stamped"
            # value equals updated_at (D-13: source column is a.updated_at).
            eq_fa = (
                await conn.execute(text("SELECT analysis_completed_at = updated_at FROM analysis WHERE file_id = :fid"), {"fid": _FA})
            ).scalar_one()
            assert eq_fa is True, "analysis_completed_at must equal updated_at (D-13 source column)"

            # (2) NAND-guard TEETH: _FC is state='analyzed' (matches the WHERE f.state='analyzed' filter) but
            #     its analysis row carries failed_at. ONLY the ``AND a.failed_at IS NULL`` guard stops the
            #     UPDATE from stamping it and tripping ck_analysis_analysis_completed_xor_failed. It stays
            #     UNCHANGED: completed NULL, failed_at preserved (VALIDATION SC-4 -- dropping the guard aborts
            #     the whole migration here, which the mutation test confirms goes RED).
            comp_fc = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FC})).scalar_one()
            failed_fc = (await conn.execute(text("SELECT failed_at FROM analysis WHERE file_id = :fid"), {"fid": _FC})).scalar_one()
            assert comp_fc is None, "analyzed+failed row must NOT be stamped (AND failed_at IS NULL guard, VALIDATION SC-4)"
            assert failed_fc is not None, "failed_at must be preserved on the guarded row"
            # NAND CheckConstraint holds -- the migration did not abort and the row is not both-set.
            nand_ok = (
                await conn.execute(
                    text("SELECT NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL) FROM analysis WHERE file_id = :fid"),
                    {"fid": _FC},
                )
            ).scalar_one()
            assert nand_ok is True, "033 NAND CheckConstraint must hold for the failed control row"

            # (3) the non-analyzed control gets nothing (WHERE state='analyzed' filter).
            comp_fd = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FD})).scalar_one()
            assert comp_fd is None, "non-analyzed (discovered) control must NOT be stamped"

            # (4) the already-done analyzed row is UNCHANGED (analysis_completed_at IS NULL predicate excludes it).
            comp_fe = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FE})).scalar_one()
            assert comp_fe is not None, "pre-completed row keeps its timestamp"

            # (5) files.state byte-unchanged (036 is READ-ONLY on files.state).
            after_state = {str(r[0]): r[1] for r in (await conn.execute(text("SELECT id, state FROM files"))).all()}
            assert after_state == before_state, "files.state must be byte-unchanged after 036"

            # count of stamped analyzed rows == 3 (_FA, _FB newly stamped + _FE already stamped).
            stamped = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM analysis a JOIN files f ON a.file_id = f.id "
                        "WHERE f.state = 'analyzed' AND a.analysis_completed_at IS NOT NULL"
                    )
                )
            ).scalar_one()
            assert stamped == 3, stamped

        # (6) IDEMPOTENCY: re-execute the exact backfill statement -- the IS NULL predicate makes it inert.
        migration_036 = _load_migration_036()
        async with engine.begin() as conn:
            await conn.execute(text(migration_036._BACKFILL_ANALYSIS_COMPLETED_AT))  # type: ignore[attr-defined]
        async with engine.connect() as conn:
            comp_fc_rerun = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FC})).scalar_one()
            comp_fd_rerun = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FD})).scalar_one()
            assert comp_fc_rerun is None and comp_fd_rerun is None, "re-run must remain inert on the untouched controls"
            eq_fa_rerun = (
                await conn.execute(text("SELECT analysis_completed_at = updated_at FROM analysis WHERE file_id = :fid"), {"fid": _FA})
            ).scalar_one()
            assert eq_fa_rerun is True, "re-run must not change the already-stamped value"

        # (7) EMPTY autogenerate diff after 036 (no ORM-mapped schema drift -- 036 is data-only).
        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_036)
        assert offenders == [], f"036 must not introduce ORM<->DB schema drift (it is data-only): {offenders}"

        # (8) downgrade is a documented NO-OP: assert it runs cleanly and changes nothing.
        async with engine.connect() as conn:
            comp_before_down = (
                await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FA})
            ).scalar_one()
        await asyncio.to_thread(downgrade_to, cfg, "035")
        async with engine.connect() as conn:
            comp_after_down = (await conn.execute(text("SELECT analysis_completed_at FROM analysis WHERE file_id = :fid"), {"fid": _FA})).scalar_one()
        assert comp_after_down == comp_before_down, "036 downgrade is a no-op -- analysis_completed_at must be unchanged"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
