"""Tests for migration 037: additive ``stage_skip`` force-skip marker sidecar (Phase 87, D-13/UI-04).

Mirrors ``test_migration_032_additive_schema.py``: static revision-id + ``saq_jobs``-banner assertions
run WITHOUT a DB (the additive-only / bare-number contract holds even where Postgres is absent); the
integration body seeds a small corpus at 036, upgrades 036 -> 037, and proves:

* the ``stage_skip`` table exists (``to_regclass('public.stage_skip')`` non-null);
* a valid enrich-stage skip row inserts;
* a second insert of the same ``(file_id, stage)`` is rejected by ``uq_stage_skip_file_stage``
  (the <=1-row invariant, D-13a / T-87-03);
* a non-enrich ``stage`` (e.g. ``'propose'``) is rejected by ``ck_stage_skip_enrich_only``
  (D-10 / T-87-02);
* ``alembic`` autogenerate against the 037 head produces an EMPTY diff for ``stage_skip``
  (the ORM ``__table_args__`` mirror parity -- PERF-01 discipline);
* the mirrored downgrade drops the table (``to_regclass`` null, T-87-04).

CRITICAL: migration 037 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).
A grep-style assertion enforces this.

Operator pre-condition for the integration body: the database ``phaze_migrations_test`` must exist
(see ``tests/integration/test_migrations/conftest.py``); run via ``just test-db`` (port 5433 --
``MIGRATIONS_TEST_DATABASE_URL`` footgun).
"""

import asyncio
import importlib.util
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
import pytest
from sqlalchemy import text
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


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "037_add_stage_skip.py"

# The 037 schema objects the empty-autogenerate-diff contract is scoped to. Unrelated pre-existing
# ORM<->DB drift (not introduced by this phase) is deliberately NOT in scope.
_O37_TABLES = {"stage_skip"}

# Fixed seed UUIDs (readable last nibble = role).
_FA = "00000000-0000-0000-0000-0000000000a0"  # gets a valid analyze skip row (+ the duplicate attempt)
_FB = "00000000-0000-0000-0000-0000000000b0"  # target of the non-enrich CHECK-violation attempt

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, 'legacy-application-server', :h, :p, :n, :p, 'flac', 1000, :st, NOW(), NOW())"
)

_INSERT_SKIP_SQL = "INSERT INTO stage_skip (id, file_id, stage, reason) VALUES (gen_random_uuid(), :fid, :stage, :reason)"


def _load_migration_037() -> object:
    """Load the 037 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_037", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_revision_identifiers_are_bare_numbers() -> None:
    """037 chains off 036 using bare-number strings (no long migration names)."""
    migration_037 = _load_migration_037()
    assert migration_037.revision == "037"  # type: ignore[attr-defined]
    assert migration_037.down_revision == "036"  # type: ignore[attr-defined]
    assert migration_037.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/031/032 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 037 must not reference saq_jobs outside its banner: {offending}"


async def _seed_file(engine, fid: str, path: str, state: str, sha: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row in the given ``state`` (FK to the 012-seeded legacy agent)."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_FILE_SQL), {"id": fid, "h": sha, "p": path, "n": path.rsplit("/", 1)[-1], "st": state})


def _diffs_touching_037(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would create/drop the 037 ``stage_skip`` table."""
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
        if op_name in ("add_table", "remove_table") and getattr(diff[1], "name", None) in _O37_TABLES:
            offenders.append((op_name, diff[1].name))
    return offenders


@pytest.mark.asyncio
async def test_upgrade_037_creates_stage_skip_enforces_constraints_empty_diff_then_downgrade() -> None:
    """037 creates ``stage_skip``, enforces UNIQUE + enrich-only CHECK, diffs empty, downgrade reverses."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(downgrade_to, cfg, "base")
    await asyncio.to_thread(upgrade_to, cfg, "036")

    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_file(engine, _FA, "/music/skip_a.flac", "analyzed", "hash-a")
        await _seed_file(engine, _FB, "/music/skip_b.flac", "analyzed", "hash-b")

        await asyncio.to_thread(upgrade_to, cfg, "037")

        # (a) the stage_skip table exists.
        async with engine.connect() as conn:
            exists = (await conn.execute(text("SELECT to_regclass('public.stage_skip')"))).scalar_one()
            assert exists is not None, "stage_skip table must exist after 037"

        # (b) a valid enrich-stage skip row inserts.
        async with engine.begin() as conn:
            await conn.execute(text(_INSERT_SKIP_SQL), {"fid": _FA, "stage": "analyze", "reason": "corrupt source"})
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT count(*) FROM stage_skip"))).scalar_one()
            assert rows == 1, rows

        # (c) a duplicate (file_id, stage) is rejected by uq_stage_skip_file_stage (<=1-row invariant).
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(_INSERT_SKIP_SQL), {"fid": _FA, "stage": "analyze", "reason": "second attempt"})

        # (d) a non-enrich stage is rejected by ck_stage_skip_enrich_only (D-10, enrich-only).
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(_INSERT_SKIP_SQL), {"fid": _FB, "stage": "propose", "reason": "not force-skippable"})

        # (e) autogenerate against the 037 head yields an EMPTY diff for stage_skip (ORM mirror parity).
        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_037)
        assert offenders == [], f"autogenerate churn on stage_skip breaks the empty-diff contract: {offenders}"

        # (f) mirrored downgrade drops the table (T-87-04).
        await asyncio.to_thread(downgrade_to, cfg, "036")
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT to_regclass('public.stage_skip')"))).scalar_one() is None, "downgrade must drop stage_skip"
    finally:
        await engine.dispose()
        await asyncio.to_thread(downgrade_to, cfg, "base")
