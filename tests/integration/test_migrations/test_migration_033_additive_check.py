"""Tests for migration 033: the analysis ``completed XOR failed`` CHECK (Phase 81, FAIL-01/D-06/D-09).

Mirrors ``test_migration_032_additive_schema.py``: static revision-id + ``saq_jobs``-banner assertions
run WITHOUT a DB; the integration body seeds a corpus at 031, walks 031 -> 032 (letting 032's REAL
unguarded backfill manufacture the mixed row), then 032 -> 033, and proves:

* the D-09 ordering holds -- the mixed row (``analysis_completed_at`` AND ``failed_at`` both set, the
  exact shape 032's ``ON CONFLICT DO UPDATE`` produces on a previously-analyzed file that later hit
  ``state='analysis_failed'``) is CLEANED before ``create_check_constraint`` validates it, so the
  upgrade succeeds at all rather than aborting on the live corpus;
* *done wins* on that cleanup (D-04) -- ``failed_at`` is nulled and ``analysis_completed_at`` is
  RETAINED, so no file's derived analyze status changes and the Phase 79 shadow gate stays green;
* the failed-only and done-only rows are left untouched;
* the rendered constraint name is ``ck_analysis_analysis_completed_xor_failed`` (bare name + the
  ``ck_%(table_name)s_%(constraint_name)s`` convention), and the ORM ``__table_args__`` mirror renders
  the identical name over the identical predicate -- the D-06 empty-autogenerate-diff contract;
* ``alembic`` autogenerate against the 033 head produces an EMPTY diff for the 033 objects;
* the CHECK actually rejects a newly-mixed row;
* down/up round-trips (downgrade drops the CHECK; re-upgrading re-adds it).

CRITICAL: migration 033 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).
A grep-style assertion enforces this.

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
from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import phaze.models  # noqa: F401  -- registers every table on Base.metadata for the autogenerate diff
from phaze.models.base import Base
from tests.integration.test_migrations.conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    downgrade_to,
    upgrade_to,
)


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "033_add_analysis_completed_xor_failed.py"

# The bare name migration 033 passes to ``create_check_constraint`` / ``drop_constraint``.
_BARE_NAME = "analysis_completed_xor_failed"
# ...and what the ``ck_%(table_name)s_%(constraint_name)s`` convention renders it to in Postgres.
_RENDERED_NAME = "ck_analysis_analysis_completed_xor_failed"

# Fixed seed UUIDs (readable last nibble = role).
_F_MIXED = "00000000-0000-0000-0000-0000000000a0"  # analysis_failed + analysis_completed_at -> 032's backfill makes it MIXED
_F_FAILED = "00000000-0000-0000-0000-0000000000b0"  # analysis_failed, no analysis row -> failed-only after 032
_F_DONE = "00000000-0000-0000-0000-0000000000c0"  # analyzed, analysis_completed_at set -> done-only, never touched

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, :st, NOW(), NOW())"
)


def _load_migration_033() -> object:
    """Load the 033 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_033", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revision_identifiers_are_bare_numbers() -> None:
    """033 chains off 032 using bare-number strings (no long migration names)."""
    migration_033 = _load_migration_033()
    assert migration_033.revision == "033"  # type: ignore[attr-defined]
    assert migration_033.down_revision == "032"  # type: ignore[attr-defined]
    assert migration_033.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/031/032 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 033 must not reference saq_jobs outside its banner: {offending}"


def test_cleanup_update_precedes_create_check_constraint() -> None:
    """D-09: the mixed-row cleanup must appear BEFORE ``create_check_constraint`` in ``upgrade()``.

    A source-order assertion, not a behavioural one -- the ordering is the whole reason this migration
    does not abort on the live corpus, and reversing the two statements would still round-trip on an
    empty test DB. Pinning the source order makes the regression impossible to introduce silently.

    Matches the *call sites* (``op.execute(...)`` / ``op.create_check_constraint(``) rather than bare
    identifiers: ``upgrade()``'s explanatory comment names ``create_check_constraint`` above the cleanup.
    """
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade_body = body.split("def upgrade() -> None:", 1)[1].split("def downgrade()", 1)[0]
    cleanup_at = upgrade_body.index("op.execute(sa.text(_CLEANUP_MIXED_ROWS))")
    check_at = upgrade_body.index("op.create_check_constraint(")
    assert cleanup_at < check_at, "the D-09 cleanup UPDATE must run BEFORE create_check_constraint"

    # The cleanup SQL clears failed_at and never mentions analysis_completed_at in a SET clause (D-04).
    cleanup_sql = _load_migration_033()._CLEANUP_MIXED_ROWS  # type: ignore[attr-defined]
    assert "SET failed_at = NULL" in cleanup_sql, cleanup_sql
    assert "SET analysis_completed_at" not in cleanup_sql, "the cleanup must never write analysis_completed_at (done wins, D-04)"


def test_orm_mirror_matches_the_migration_constraint() -> None:
    """The ORM ``__table_args__`` CHECK renders the same name + predicate the migration creates (D-06)."""
    checks = [c for c in Base.metadata.tables["analysis"].constraints if isinstance(c, CheckConstraint)]
    named = [c for c in checks if c.name == _RENDERED_NAME]
    assert len(named) == 1, f"expected exactly one {_RENDERED_NAME} CHECK on the analysis ORM model, got {[c.name for c in checks]}"
    assert str(named[0].sqltext) == "NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)"


async def _seed_file(engine, fid: str, path: str, state: str, sha: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row in the given ``state`` (FK to the 012-seeded legacy agent)."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_FILE_SQL), {"id": fid, "h": sha, "p": path, "n": path.rsplit("/", 1)[-1], "st": state})


async def _seed_corpus_at_031(engine) -> None:  # type: ignore[no-untyped-def]
    """Seed the three analyze-stage shapes at revision 031 (before ``failed_at`` exists)."""
    await _seed_file(engine, _F_MIXED, "/music/mixed.flac", "analysis_failed", "hash-mixed")
    await _seed_file(engine, _F_FAILED, "/music/failed.flac", "analysis_failed", "hash-failed")
    await _seed_file(engine, _F_DONE, "/music/done.flac", "analyzed", "hash-done")
    async with engine.begin() as conn:
        # _F_MIXED already completed a successful analysis, then later flipped to ANALYSIS_FAILED.
        # 032's unguarded ``ON CONFLICT (file_id) DO UPDATE`` backfill stamps failed_at onto this row
        # WITHOUT clearing analysis_completed_at -- that is exactly how the live-corpus mixed rows arose.
        await conn.execute(
            text(
                "INSERT INTO analysis (id, file_id, analysis_completed_at, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :fid, NOW(), NOW(), NOW())"
            ),
            {"fid": _F_MIXED},
        )
        # _F_DONE is a plain successful analysis -- never touched by the backfill or the cleanup.
        await conn.execute(
            text(
                "INSERT INTO analysis (id, file_id, analysis_completed_at, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :fid, NOW(), NOW(), NOW())"
            ),
            {"fid": _F_DONE},
        )
        # _F_FAILED gets NO analysis row -- 032's backfill INSERTs one carrying only failed_at.


def _diffs_touching_033(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would add/remove the 033 CHECK (D-06 empty-diff scope).

    Note: alembic's ``compare_metadata`` does not emit CHECK-constraint diffs, so this assertion alone
    cannot catch an ORM/migration divergence. ``test_orm_mirror_matches_the_migration_constraint`` plus
    the ``pg_constraint`` name assertion below are the substantive parity gates; this one guards against
    the CHECK's arrival dragging *other* churn (a renamed table/column/index) into the diff.
    """
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
        obj_name = getattr(diff[1], "name", None) if len(diff) > 1 else None
        if obj_name in (_BARE_NAME, _RENDERED_NAME):
            offenders.append((op_name, str(obj_name)))
        elif op_name in ("add_table", "remove_table") and obj_name == "analysis":
            offenders.append((op_name, "analysis"))
    return offenders


@pytest.mark.asyncio
async def test_upgrade_033_cleans_mixed_rows_before_check_then_downgrade_reverses() -> None:
    """033 cleans 032's mixed rows (done wins), adds the CHECK, diffs empty, and round-trips."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "031")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_corpus_at_031(engine)

        # 032's REAL backfill manufactures the mixed row (this is the D-09 premise, not a test fixture).
        await asyncio.to_thread(upgrade_to, cfg, "032")
        async with engine.connect() as conn:
            mixed = (await conn.execute(text("SELECT analysis_completed_at, failed_at FROM analysis WHERE file_id = :fid"), {"fid": _F_MIXED})).one()
            assert mixed[0] is not None and mixed[1] is not None, f"032's unguarded backfill must leave _F_MIXED mixed: {mixed}"

        # (1) D-09: the upgrade SUCCEEDS despite the pre-existing mixed row (cleanup precedes the CHECK).
        await asyncio.to_thread(upgrade_to, cfg, "033")

        async with engine.connect() as conn:
            # (2) D-04: done wins -- failed_at nulled, analysis_completed_at retained.
            mixed = (await conn.execute(text("SELECT analysis_completed_at, failed_at FROM analysis WHERE file_id = :fid"), {"fid": _F_MIXED})).one()
            assert mixed[1] is None, f"the cleanup must null failed_at on the mixed row: {mixed}"
            assert mixed[0] is not None, f"the cleanup must RETAIN analysis_completed_at (done wins, D-04): {mixed}"

            # (3) the failed-only row keeps its marker; the done-only row is untouched.
            failed = (
                await conn.execute(text("SELECT analysis_completed_at, failed_at FROM analysis WHERE file_id = :fid"), {"fid": _F_FAILED})
            ).one()
            assert failed[0] is None and failed[1] is not None, f"failed-only row must keep failed_at: {failed}"
            done = (await conn.execute(text("SELECT analysis_completed_at, failed_at FROM analysis WHERE file_id = :fid"), {"fid": _F_DONE})).one()
            assert done[0] is not None and done[1] is None, f"done-only row must be untouched: {done}"

            # (4) the CHECK exists under the convention-rendered name.
            conname = (
                await conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE contype = 'c' AND conrelid = 'analysis'::regclass AND conname = :n"),
                    {"n": _RENDERED_NAME},
                )
            ).scalar_one_or_none()
            assert conname == _RENDERED_NAME, f"expected the CHECK to render as {_RENDERED_NAME}, found {conname}"

        # (5) the CHECK actually rejects a newly-mixed row.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE analysis SET failed_at = NOW() WHERE file_id = :fid"), {"fid": _F_DONE})

        # (6) D-06: autogenerate against the 033 head yields an EMPTY diff for the 033 objects.
        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_033)
        assert offenders == [], f"autogenerate churn on 033 objects breaks the empty-diff contract (D-06): {offenders}"

        # (7) downgrade drops the CHECK (the D-09 cleanup is deliberately NOT reversed).
        await asyncio.to_thread(downgrade_to, cfg, "032")
        async with engine.connect() as conn:
            gone = (
                await conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE contype = 'c' AND conrelid = 'analysis'::regclass AND conname = :n"),
                    {"n": _RENDERED_NAME},
                )
            ).scalar_one_or_none()
            assert gone is None, "downgrade must drop the XOR CHECK"

        # (8) round-trip: re-upgrading re-adds it (the cleanup UPDATE is idempotent / a no-op now).
        await asyncio.to_thread(upgrade_to, cfg, "033")
        async with engine.connect() as conn:
            back = (
                await conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE contype = 'c' AND conrelid = 'analysis'::regclass AND conname = :n"),
                    {"n": _RENDERED_NAME},
                )
            ).scalar_one_or_none()
            assert back == _RENDERED_NAME, "re-upgrading must re-add the XOR CHECK"

        # 032's downgrade restores the 6-member cloud_job CHECK and 029's re-imposes ``s3_key NOT NULL``;
        # clear any backfilled sidecar rows before the teardown walks back to base (029/032 precedent).
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM cloud_job"))
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
