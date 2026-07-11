"""Tests for migration 038: reattribute legacy-owned rows + delete the sentinel (Phase 89, LEGACY-02/03, D-01..D-10).

Mirrors ``test_024`` / ``test_migration_035`` for the static contract (bare-number revision ids, the
``saq_jobs``-never-referenced grep guard) but DEVIATES from every prior migration test in two ways
demanded by 038's design:

* the downgrade is deliberately IRREVERSIBLE -- ``downgrade_to(cfg, "037")`` must raise
  ``NotImplementedError`` (D-10), so there is NO reversibility mirror; and
* because a successful 038 cannot be walked back, every integration body tears down via a raw
  ``_reset_schema`` (DROP/CREATE ``public``) rather than ``downgrade_to("base")`` -- otherwise teardown
  would trip 038's ``NotImplementedError``.

The integration bodies drive ``_reset_schema`` -> ``upgrade_to("037")`` -> seed -> ``upgrade_to("038")``
(all sync alembic calls via ``asyncio.to_thread``) and prove the 8 scenarios: reattribution + sentinel
delete, the Pitfall-1 live-batch DELETE (no ``uq_scan_batches_agent_id_live`` collision), the 0/>1
fileserver aborts (single-txn rollback), the ``-x reattribute_to`` override, the ``NotImplementedError``
downgrade, and an empty autogenerate diff (038 adds no schema).

CRITICAL: migration 038 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).

FOOTGUN: this test targets the migrations DB on port **5433** (``just test-db`` provisioning), NOT the
5432 default baked into ``conftest.MIGRATIONS_TEST_DATABASE_URL``. Run it via::

    MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test" \\
    just test-bucket integration

``just test-bucket`` does NOT export ``MIGRATIONS_TEST_DATABASE_URL`` -- export it explicitly or the
migration harness silently talks to the wrong (5432) DB and the test fails like an infra flake.
"""

import argparse
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
    _reset_schema,
    downgrade_to,
    upgrade_to,
)


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "038_retire_legacy_sentinel.py"

_LEGACY = "legacy-application-server"

# Fixed ids (agent ids must satisfy ck_agents_id_charset: ^[a-z0-9]+(-[a-z0-9]+)*$).
_NOX = "nox"
_OTHER = "other-fileserver"

# Fixed file/scan_batch UUIDs (readable last nibble = role).
_FILE_A = "00000000-0000-0000-0000-0000000000a0"
_FILE_B = "00000000-0000-0000-0000-0000000000a1"
_BATCH_DONE = "00000000-0000-0000-0000-0000000000b0"  # legacy non-live batch -> reattributed
_BATCH_NOX_LIVE = "00000000-0000-0000-0000-0000000000b2"  # target's own live batch -> untouched
# (the legacy status='live' watcher batch is not a fixture id: migration 012 already seeds exactly one.)

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, :aid, :h, :p, :n, :p, 'flac', 1000, 'analyzed', NOW(), NOW())"
)
_SEED_BATCH_SQL = (
    "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files, "
    "created_at, updated_at) VALUES (:id, :aid, :sp, :status, 0, 0, NOW(), NOW())"
)
_SEED_AGENT_SQL = "INSERT INTO agents (id, name, kind, revoked_at, created_at, updated_at) VALUES (:id, :id, 'fileserver', :revoked, NOW(), NOW())"


def _load_migration_038() -> object:
    """Load the 038 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_038", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------------------
# Static, DB-free assertions
# --------------------------------------------------------------------------------------------------


def test_revision_identifiers_are_bare_numbers() -> None:
    """038 chains off 037 using bare-number strings (no long migration names)."""
    migration_038 = _load_migration_038()
    assert migration_038.revision == "038"  # type: ignore[attr-defined]
    assert migration_038.down_revision == "037"  # type: ignore[attr-defined]
    assert migration_038.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/031/032 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 038 must not reference saq_jobs outside its banner: {offending}"


def test_target_id_is_never_f_string_interpolated() -> None:
    """The reattribution target is passed via ``bindparams(:target)`` -- never f-stringed into SQL (T-89-02-01)."""
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert ".bindparams(target=target)" in body
    assert ":target" in body
    # No f-string SQL surface reaches the reattribution statements.
    assert "f'''" not in body and 'f"""' not in body


# --------------------------------------------------------------------------------------------------
# Seed helpers
# --------------------------------------------------------------------------------------------------


async def _seed_agent(engine, aid: str, revoked: bool = False) -> None:  # type: ignore[no-untyped-def]
    """Insert a ``kind='fileserver'`` agent; ``revoked=True`` stamps ``revoked_at=NOW()`` (excluded by auto-detect)."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_AGENT_SQL.replace(":revoked", "NOW()" if revoked else "NULL")), {"id": aid})


async def _seed_file(engine, fid: str, agent_id: str, path: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row owned by ``agent_id``."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_FILE_SQL), {"id": fid, "aid": agent_id, "h": f"hash-{fid[-2:]}", "p": path, "n": path.rsplit("/", 1)[-1]})


async def _seed_batch(engine, bid: str, agent_id: str, status: str, path: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a ``scan_batches`` row owned by ``agent_id`` with the given status."""
    async with engine.begin() as conn:
        await conn.execute(text(_SEED_BATCH_SQL), {"id": bid, "aid": agent_id, "sp": path, "status": status})


async def _count(engine, sql: str, params: dict | None = None) -> int:  # type: ignore[no-untyped-def]
    """Run a scalar COUNT query."""
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params or {})).scalar_one())


# --------------------------------------------------------------------------------------------------
# Scenarios 1 + 2: reattribution moves rows, sentinel deleted
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_038_reattributes_rows_and_deletes_sentinel() -> None:
    """Scenario 1/2: legacy-owned files + non-live batch move to the sole fileserver; the sentinel is deleted."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_agent(engine, _NOX)
        await _seed_file(engine, _FILE_A, _LEGACY, "/music/legacy_a.flac")
        await _seed_file(engine, _FILE_B, _LEGACY, "/music/legacy_b.flac")
        await _seed_batch(engine, _BATCH_DONE, _LEGACY, "completed", "/music")

        await asyncio.to_thread(upgrade_to, cfg, "038")

        # (1) the legacy-owned rows now carry agent_id=nox; zero legacy-owned rows remain.
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _NOX}) == 2
        assert await _count(engine, "SELECT count(*) FROM scan_batches WHERE agent_id = :a", {"a": _NOX}) == 1
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _LEGACY}) == 0
        assert await _count(engine, "SELECT count(*) FROM scan_batches WHERE agent_id = :a", {"a": _LEGACY}) == 0
        # (2) the sentinel agent row is deleted.
        assert await _count(engine, "SELECT count(*) FROM agents WHERE id = :a", {"a": _LEGACY}) == 0
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario 3: live-batch collision (Pitfall 1) -- the legacy live batch is DELETED, not reattributed
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_038_deletes_legacy_live_batch_without_unique_collision() -> None:
    """Scenario 3: with BOTH a legacy live batch and the target's own live batch, 038 does not raise and the legacy live batch is gone."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_agent(engine, _NOX)
        # The legacy status='live' watcher batch already exists -- migration 012 seeds exactly one. Seeding
        # a second would itself collide on uq_scan_batches_agent_id_live, so we rely on the real one.
        assert await _count(engine, "SELECT count(*) FROM scan_batches WHERE agent_id = :a AND status = 'live'", {"a": _LEGACY}) == 1
        await _seed_batch(engine, _BATCH_NOX_LIVE, _NOX, "live", "<watcher>")  # target's own live batch
        await _seed_batch(engine, _BATCH_DONE, _LEGACY, "completed", "/music")  # legacy non-live -> reattribute

        # Must NOT raise (the DELETE-first ordering avoids the uq_scan_batches_agent_id_live collision).
        await asyncio.to_thread(upgrade_to, cfg, "038")

        # The legacy live watcher batch is gone (DELETED, not reattributed); nox keeps exactly its own live batch.
        assert await _count(engine, "SELECT count(*) FROM scan_batches WHERE agent_id = :a AND status = 'live'", {"a": _LEGACY}) == 0
        assert await _count(engine, "SELECT count(*) FROM scan_batches WHERE agent_id = :a AND status = 'live'", {"a": _NOX}) == 1
        # The non-live legacy batch reattributed to nox; zero legacy-owned batches remain.
        assert await _count(engine, "SELECT count(*) FROM scan_batches WHERE agent_id = :a", {"a": _LEGACY}) == 0
        assert await _count(engine, "SELECT count(*) FROM agents WHERE id = :a", {"a": _LEGACY}) == 0
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario 4: abort on zero fileserver -- single-txn rollback leaves the sentinel intact
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_038_aborts_and_rolls_back_when_no_fileserver() -> None:
    """Scenario 4: with no non-revoked fileserver, 038 raises and the sentinel + its rows survive (rollback proof)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        # Only the (revoked) legacy agent exists -- no non-revoked fileserver to reattribute to.
        await _seed_file(engine, _FILE_A, _LEGACY, "/music/legacy_a.flac")

        with pytest.raises(RuntimeError):
            await asyncio.to_thread(upgrade_to, cfg, "038")

        # Rollback proof: the sentinel agent + its file are untouched.
        assert await _count(engine, "SELECT count(*) FROM agents WHERE id = :a", {"a": _LEGACY}) == 1
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _LEGACY}) == 1
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario 5: abort on >1 fileserver (no override) -- message points at -x reattribute_to
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_038_aborts_on_multiple_fileservers_without_override() -> None:
    """Scenario 5: two non-revoked fileservers and no override -> abort with the ``-x reattribute_to`` guidance."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_agent(engine, _NOX)
        await _seed_agent(engine, _OTHER)
        await _seed_file(engine, _FILE_A, _LEGACY, "/music/legacy_a.flac")

        with pytest.raises(RuntimeError, match=r"pass -x reattribute_to"):
            await asyncio.to_thread(upgrade_to, cfg, "038")

        # Rollback proof: nothing was reattributed or deleted.
        assert await _count(engine, "SELECT count(*) FROM agents WHERE id = :a", {"a": _LEGACY}) == 1
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _LEGACY}) == 1
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario 6: -x reattribute_to override selects the target among multiple fileservers
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_038_x_override_selects_target_among_multiple_fileservers() -> None:
    """Scenario 6: with two fileservers, ``-x reattribute_to=nox`` (via ``cfg.cmd_opts``) reattributes to the chosen id.

    Harness (A2, no in-repo precedent): ``get_x_argument`` reads ``config.cmd_opts.x``; setting
    ``cfg.cmd_opts = argparse.Namespace(x=[...])`` before ``command.upgrade`` surfaces the override to the
    migration. This validates the chosen mechanism -- no ``env.py`` fallback was required.
    """
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_agent(engine, _NOX)
        await _seed_agent(engine, _OTHER)
        await _seed_file(engine, _FILE_A, _LEGACY, "/music/legacy_a.flac")

        # Surface -x reattribute_to=nox to the migration's context.get_x_argument.
        cfg.cmd_opts = argparse.Namespace(x=[f"reattribute_to={_NOX}"])
        await asyncio.to_thread(upgrade_to, cfg, "038")

        # Reattributed to the OVERRIDE target (nox), not the other fileserver; sentinel deleted.
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _NOX}) == 1
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _OTHER}) == 0
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _LEGACY}) == 0
        assert await _count(engine, "SELECT count(*) FROM agents WHERE id = :a", {"a": _LEGACY}) == 0
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


@pytest.mark.asyncio
async def test_038_x_override_rejects_invalid_target() -> None:
    """A ``-x reattribute_to`` id that is not a valid non-revoked fileserver aborts (validated, not trusted)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_agent(engine, _NOX)
        await _seed_file(engine, _FILE_A, _LEGACY, "/music/legacy_a.flac")

        cfg.cmd_opts = argparse.Namespace(x=["reattribute_to=does-not-exist"])
        with pytest.raises(RuntimeError, match=r"not a valid non-revoked fileserver"):
            await asyncio.to_thread(upgrade_to, cfg, "038")

        # Rollback proof: the sentinel + its file are untouched.
        assert await _count(engine, "SELECT count(*) FROM agents WHERE id = :a", {"a": _LEGACY}) == 1
        assert await _count(engine, "SELECT count(*) FROM files WHERE agent_id = :a", {"a": _LEGACY}) == 1
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario 7: NON-reversible downgrade -- DEVIATES from every prior migration test
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_038_downgrade_raises_not_implemented() -> None:
    """Scenario 7: downgrade is irreversible -- ``downgrade_to("037")`` raises ``NotImplementedError`` (D-10)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_agent(engine, _NOX)
        await _seed_file(engine, _FILE_A, _LEGACY, "/music/legacy_a.flac")
        await asyncio.to_thread(upgrade_to, cfg, "038")

        with pytest.raises(NotImplementedError):
            await asyncio.to_thread(downgrade_to, cfg, "037")
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario 8: empty autogenerate diff -- 038 adds no schema (mirrors test_035's empty-scope parity)
# --------------------------------------------------------------------------------------------------

# 038 is a pure DATA migration: it adds NO ORM-mapped schema, so the empty-diff assertion is scoped to
# the (empty) set of objects 038 would create. Unrelated pre-existing ORM<->DB drift is deliberately out
# of scope (identical scoping to test_migration_035).
_O38_TABLES: set[str] = set()
_O38_INDEXES: set[str] = set()
_O38_COLUMNS: set[tuple[str, str]] = set()


def _diffs_touching_038(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would create/drop a 038-scoped object (empty-diff scope)."""
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
        if (op_name in ("add_table", "remove_table") and getattr(diff[1], "name", None) in _O38_TABLES) or (
            op_name in ("add_index", "remove_index") and getattr(diff[1], "name", None) in _O38_INDEXES
        ):
            offenders.append((op_name, diff[1].name))
        elif op_name in ("add_column", "remove_column") and (diff[2], diff[3].name) in _O38_COLUMNS:
            offenders.append((op_name, f"{diff[2]}.{diff[3].name}"))
    return offenders


@pytest.mark.asyncio
async def test_038_autogenerate_diff_is_empty() -> None:
    """Scenario 8: 038 introduces no new tables/indexes/columns -- autogenerate against the 038 head is empty."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    try:
        await _seed_agent(engine, _NOX)
        await _seed_file(engine, _FILE_A, _LEGACY, "/music/legacy_a.flac")
        await asyncio.to_thread(upgrade_to, cfg, "038")

        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_038)
        assert offenders == [], f"038 must not introduce ORM<->DB schema drift (it is data-only): {offenders}"
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
