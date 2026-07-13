"""Tests for migration 039: guarded, reversible DROP of the dead ``files.state`` column (Phase 90, MIG-04, PR-C).

This is the phase's ONLY destructive migration. PR-A made every ``files.state`` reader derived and PR-B
removed every writer, so the column is dead. ``039`` archives ``files.state`` into ``files_state_archive``,
re-runs the ``032`` marker delta idempotently, then drops ``ix_files_state`` + ``files.state`` under a
``lock_timeout`` + savepoint-retry wrapper -- but ONLY after an inline guard proves the corpus is drained
and shadow-compare-consistent (D-06/D-07). ``downgrade()`` restores the column VERBATIM from the archive
(D-10). It NEVER imports ``phaze.services.*`` and NEVER references ``saq_jobs`` / the scheduling ledger.

Mirrors ``test_migration_038`` for the static contract (bare-number revision ids, the
``saq_jobs``-never-referenced grep, the "no f-string SQL" scan) and drives the same
``_reset_schema -> upgrade_to("037") -> seed fileserver -> upgrade_to("038") -> seed corpus`` chain the
harness demands (migration 038 aborts a fileserver-less ``upgrade head`` -- Phase 89 D-01). The five
integration bodies prove: (a) HAPPY -- a drained, shadow-compare-consistent corpus drops cleanly, the
column + index are GONE and ``files_state_archive`` holds one verbatim row per file; (b) EMPTY -- a
files-less DB passes cleanly (D-06, no CR-02 fresh-DB abort); (c) VIOLATION -- a shadow-compare
implication violation (``state='analyzed'`` with no completed analysis) raises + rolls back; (d)
MID-FLIGHT -- a ``state='pushing'`` file OR a non-terminal ``cloud_job`` raises + rolls back; (e)
DOWNGRADE -- ``downgrade_to("038")`` recreates the column + index and restores durable states VERBATIM
from the archive, and a subsequent re-upgrade round-trips.

CRITICAL: migration 039 must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).

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
import re

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
    _seed_fileserver,
    downgrade_to,
    upgrade_to,
)


_MIGRATION_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "039_drop_files_state_column.py"

# The fileserver conftest seeds between 037 and 038; reuse it as the owning agent for every seeded file.
_AGENT = "test-fileserver"

# Fixed file UUIDs (readable last nibble = role).
_F_META = "00000000-0000-0000-0000-0000000000c0"
_F_ANALYZED = "00000000-0000-0000-0000-0000000000c1"
_F_AFAILED = "00000000-0000-0000-0000-0000000000c2"
_F_PROP = "00000000-0000-0000-0000-0000000000c3"
_F_APPROVED = "00000000-0000-0000-0000-0000000000c4"
_F_REJECTED = "00000000-0000-0000-0000-0000000000c5"
_F_EXECUTED = "00000000-0000-0000-0000-0000000000c6"
_F_DEDUP = "00000000-0000-0000-0000-0000000000c7"
_F_AWAITING = "00000000-0000-0000-0000-0000000000c8"
_F_DISCOVERED = "00000000-0000-0000-0000-0000000000c9"
_F_FINGERPRINTED = "00000000-0000-0000-0000-0000000000ca"
_F_LOCAL = "00000000-0000-0000-0000-0000000000cb"

# The drained, shadow-compare-consistent HAPPY corpus: every DURABLE state paired with the derived row
# its hard invariant requires, plus the vacuous (discovered) and soft-allowlisted (fingerprinted,
# local_analyzing) states that carry NO derived marker and must still pass the guard.
_DURABLE_STATES: dict[str, str] = {
    _F_META: "metadata_extracted",
    _F_ANALYZED: "analyzed",
    _F_AFAILED: "analysis_failed",
    _F_PROP: "proposal_generated",
    _F_APPROVED: "approved",
    _F_REJECTED: "rejected",
    _F_EXECUTED: "executed",
    _F_DEDUP: "duplicate_resolved",
    _F_AWAITING: "awaiting_cloud",
}
_SOFT_STATES: dict[str, str] = {
    _F_DISCOVERED: "discovered",
    _F_FINGERPRINTED: "fingerprinted",
    _F_LOCAL: "local_analyzing",
}

_SEED_FILE_SQL = (
    "INSERT INTO files (id, agent_id, sha256_hash, original_path, original_filename, current_path, "
    "file_type, file_size, state, created_at, updated_at) "
    "VALUES (:id, :aid, :h, :p, :n, :p, 'flac', 1000, :state, NOW(), NOW())"
)
_SEED_ANALYSIS_COMPLETED = "INSERT INTO analysis (id, file_id, analysis_completed_at) VALUES (gen_random_uuid(), :fid, NOW())"
_SEED_ANALYSIS_FAILED = "INSERT INTO analysis (id, file_id, failed_at, error_message) VALUES (gen_random_uuid(), :fid, NOW(), 'seed-fail')"
_SEED_METADATA_DONE = "INSERT INTO metadata (id, file_id, failed_at) VALUES (gen_random_uuid(), :fid, NULL)"
_SEED_PROPOSAL = "INSERT INTO proposals (id, file_id, proposed_filename, status) VALUES (gen_random_uuid(), :fid, 'x.flac', :status)"
_SEED_CLOUD_JOB = "INSERT INTO cloud_job (id, file_id, status) VALUES (gen_random_uuid(), :fid, :status)"
_SEED_DEDUP = "INSERT INTO dedup_resolution (id, file_id) VALUES (gen_random_uuid(), :fid)"


def _load_migration_039() -> object:
    """Load the 039 migration module by path (its name starts with a digit)."""
    spec = importlib.util.spec_from_file_location("migration_039", _MIGRATION_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------------------
# Static, DB-free assertions
# --------------------------------------------------------------------------------------------------


def test_revision_identifiers_are_bare_numbers() -> None:
    """039 chains off 038 using bare-number strings (no long migration names)."""
    migration_039 = _load_migration_039()
    assert migration_039.revision == "039"  # type: ignore[attr-defined]
    assert migration_039.down_revision == "038"  # type: ignore[attr-defined]
    assert migration_039.branch_labels is None  # type: ignore[attr-defined]


def test_migration_never_references_saq_jobs() -> None:
    """SAQ owns ``saq_jobs`` -- the migration body must not touch it (020/031/032 CRITICAL banner)."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "saq_jobs" in line and not line.lstrip().startswith("#") and "never reference" not in line.lower()]
    assert not offending, f"migration 039 must not reference saq_jobs outside its banner: {offending}"


def test_migration_never_references_scheduling_ledger() -> None:
    """D-07: the frozen-in-time migration derives ONLY from durable output tables, never the ledger."""
    body_lines = _MIGRATION_PATH.read_text(encoding="utf-8").splitlines()
    offending = [line for line in body_lines if "scheduling_ledger" in line and not line.lstrip().startswith("#") and "never" not in line.lower()]
    assert not offending, f"migration 039 must not reference scheduling_ledger outside its banner: {offending}"


def test_migration_does_not_import_phaze() -> None:
    """D-07: no ``import phaze`` / ``from phaze`` -- the migration is frozen against future model drift."""
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "import phaze" not in body
    assert "from phaze" not in body


def test_no_f_string_interpolated_sql() -> None:
    """All SQL operands are fixed literals in ``sa.text`` constants -- never an f-string SQL surface (T-90-sqli).

    Precise, not a blunt f-string ban: error-message f-strings are legitimate. This flags a triple-quoted
    f-string (a multi-line SQL f-string) OR a single-line f-string that embeds a SQL command keyword (an
    interpolated-operand SQL surface). Every syntactic form is covered (multi-line + single-line).
    """
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "f'''" not in body and 'f"""' not in body, "no triple-quoted f-string SQL"
    sql_kw = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|SET LOCAL)\b")
    fstring = re.compile(r"""f(?:"|')""")
    offenders = [ln.strip() for ln in body.splitlines() if fstring.search(ln) and sql_kw.search(ln)]
    assert not offenders, f"f-string SQL surface (interpolated operand) found: {offenders}"


def test_archive_table_named_consistently() -> None:
    """The forensic archive table name appears for create + insert + downgrade restore (D-10)."""
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert body.count("files_state_archive") >= 3


def test_ddl_drop_is_lock_timeout_wrapped() -> None:
    """The ACCESS EXCLUSIVE drop runs under a per-attempt SAVEPOINT + SET LOCAL lock_timeout (Pattern 1)."""
    body = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "begin_nested" in body
    assert "lock_timeout" in body


# --------------------------------------------------------------------------------------------------
# Seed helpers
# --------------------------------------------------------------------------------------------------


async def _exec(engine, sql: str, params: dict) -> None:  # type: ignore[no-untyped-def]
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _seed_file(engine, fid: str, state: str) -> None:  # type: ignore[no-untyped-def]
    """Insert a minimal ``files`` row owned by the seeded fileserver, at ``state``."""
    await _exec(
        engine,
        _SEED_FILE_SQL,
        {"id": fid, "aid": _AGENT, "h": f"hash-{fid[-2:]}", "p": f"/music/{fid[-2:]}.flac", "n": f"{fid[-2:]}.flac", "state": state},
    )


async def _count(engine, sql: str, params: dict | None = None) -> int:  # type: ignore[no-untyped-def]
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params or {})).scalar_one())


async def _column_exists(engine, table: str, column: str) -> bool:  # type: ignore[no-untyped-def]
    return (
        await _count(
            engine,
            "SELECT count(*) FROM information_schema.columns WHERE table_name = :t AND column_name = :c",
            {"t": table, "c": column},
        )
        > 0
    )


async def _index_exists(engine, index: str) -> bool:  # type: ignore[no-untyped-def]
    return await _count(engine, "SELECT count(*) FROM pg_indexes WHERE indexname = :i", {"i": index}) > 0


async def _table_exists(engine, table: str) -> bool:  # type: ignore[no-untyped-def]
    return await _count(engine, "SELECT count(*) FROM information_schema.tables WHERE table_name = :t", {"t": table}) > 0


async def _seed_consistent_corpus(engine) -> None:  # type: ignore[no-untyped-def]
    """Seed the drained, shadow-compare-consistent HAPPY corpus (every durable state + its derived row)."""
    for fid, state in {**_DURABLE_STATES, **_SOFT_STATES}.items():
        await _seed_file(engine, fid, state)
    await _exec(engine, _SEED_METADATA_DONE, {"fid": _F_META})
    await _exec(engine, _SEED_ANALYSIS_COMPLETED, {"fid": _F_ANALYZED})
    await _exec(engine, _SEED_ANALYSIS_FAILED, {"fid": _F_AFAILED})
    await _exec(engine, _SEED_PROPOSAL, {"fid": _F_PROP, "status": "pending"})
    await _exec(engine, _SEED_PROPOSAL, {"fid": _F_APPROVED, "status": "approved"})
    await _exec(engine, _SEED_PROPOSAL, {"fid": _F_REJECTED, "status": "rejected"})
    await _exec(engine, _SEED_PROPOSAL, {"fid": _F_EXECUTED, "status": "executed"})
    await _exec(engine, _SEED_DEDUP, {"fid": _F_DEDUP})
    await _exec(engine, _SEED_CLOUD_JOB, {"fid": _F_AWAITING, "status": "awaiting"})


async def _chain_to_038(cfg) -> object:  # type: ignore[no-untyped-def]
    """Drive _reset -> 037 -> seed fileserver -> 038 and return a fresh engine (the harness chain)."""
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "037")
    await _seed_fileserver(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "038")
    return create_async_engine(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario (a) HAPPY: a drained, consistent corpus drops cleanly; column + index gone; archive verbatim
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_039_drops_column_and_archives_on_consistent_corpus() -> None:
    """HAPPY: 039 drops ix_files_state + files.state and archives every row's state verbatim."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = await _chain_to_038(cfg)
    try:
        await _seed_consistent_corpus(engine)
        expected = {**_DURABLE_STATES, **_SOFT_STATES}

        await asyncio.to_thread(upgrade_to, cfg, "039")

        # The dead column + its index are GONE.
        assert not await _column_exists(engine, "files", "state")
        assert not await _index_exists(engine, "ix_files_state")
        # The forensic archive holds one verbatim row per file.
        assert await _table_exists(engine, "files_state_archive")
        assert await _count(engine, "SELECT count(*) FROM files_state_archive") == len(expected)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT file_id, state FROM files_state_archive"))).all()
        archived = {str(fid): st for fid, st in rows}
        assert archived == expected
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario (b) EMPTY: a files-less DB passes cleanly (D-06 -- no CR-02 fresh-DB abort)
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_039_passes_cleanly_on_empty_files_table() -> None:
    """EMPTY: with zero files, every guard COUNT is 0, so 039 drops cleanly (no fresh-DB abort)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = await _chain_to_038(cfg)
    try:
        assert await _count(engine, "SELECT count(*) FROM files") == 0

        await asyncio.to_thread(upgrade_to, cfg, "039")

        assert not await _column_exists(engine, "files", "state")
        assert not await _index_exists(engine, "ix_files_state")
        assert await _table_exists(engine, "files_state_archive")
        assert await _count(engine, "SELECT count(*) FROM files_state_archive") == 0
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario (c) VIOLATION: a shadow-compare implication violation raises + rolls back
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_039_raises_on_shadow_compare_violation_and_rolls_back() -> None:
    """VIOLATION: state='analyzed' with NO completed analysis trips the hard-invariant guard -> raise + rollback."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = await _chain_to_038(cfg)
    try:
        await _seed_file(engine, _F_ANALYZED, "analyzed")  # no analysis row -> analyzed⇒done violated

        with pytest.raises(RuntimeError):
            await asyncio.to_thread(upgrade_to, cfg, "039")

        # Rollback proof: the column survives and the archive was never created.
        assert await _column_exists(engine, "files", "state")
        assert not await _table_exists(engine, "files_state_archive")
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario (d) MID-FLIGHT: a pushing state OR a non-terminal cloud_job raises + rolls back
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_039_raises_on_pushing_state_and_rolls_back() -> None:
    """MID-FLIGHT (state limb): a file at state='pushing' aborts the drop (must deploy under --profile drain)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = await _chain_to_038(cfg)
    try:
        await _seed_file(engine, _F_AWAITING, "pushing")
        await _exec(engine, _SEED_CLOUD_JOB, {"fid": _F_AWAITING, "status": "uploading"})  # shadow-compare satisfied; still mid-flight

        with pytest.raises(RuntimeError):
            await asyncio.to_thread(upgrade_to, cfg, "039")

        assert await _column_exists(engine, "files", "state")
        assert not await _table_exists(engine, "files_state_archive")
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


@pytest.mark.asyncio
async def test_039_raises_on_inflight_cloud_job_and_rolls_back() -> None:
    """MID-FLIGHT (cloud_job limb): a non-terminal cloud_job (status='running') aborts the drop, isolated from state."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = await _chain_to_038(cfg)
    try:
        # state='pushed' is NOT in the files.state mid-flight list, so ONLY the running cloud_job trips the guard.
        await _seed_file(engine, _F_AWAITING, "pushed")
        await _exec(engine, _SEED_CLOUD_JOB, {"fid": _F_AWAITING, "status": "running"})

        with pytest.raises(RuntimeError):
            await asyncio.to_thread(upgrade_to, cfg, "039")

        assert await _column_exists(engine, "files", "state")
        assert not await _table_exists(engine, "files_state_archive")
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario (e) DOWNGRADE: recreate column + index, restore verbatim from archive, and round-trip up again
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_039_downgrade_restores_state_verbatim_and_round_trips() -> None:
    """DOWNGRADE (D-10): downgrade recreates the column + index and restores durable states VERBATIM; re-upgrade round-trips."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = await _chain_to_038(cfg)
    try:
        await _seed_consistent_corpus(engine)
        expected = {**_DURABLE_STATES, **_SOFT_STATES}

        await asyncio.to_thread(upgrade_to, cfg, "039")
        assert not await _column_exists(engine, "files", "state")

        await asyncio.to_thread(downgrade_to, cfg, "038")

        # The column + index are back and every seeded state is restored VERBATIM from the archive.
        assert await _column_exists(engine, "files", "state")
        assert await _index_exists(engine, "ix_files_state")
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id, state FROM files"))).all()
        restored = {str(fid): st for fid, st in rows}
        assert restored == expected
        # The consumed archive is dropped on downgrade so the round-trip up can recreate it cleanly.
        assert not await _table_exists(engine, "files_state_archive")

        # Round-trip: a second upgrade succeeds (archive re-created + repopulated, drop re-applied).
        await asyncio.to_thread(upgrade_to, cfg, "039")
        assert not await _column_exists(engine, "files", "state")
        assert await _count(engine, "SELECT count(*) FROM files_state_archive") == len(expected)
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


# --------------------------------------------------------------------------------------------------
# Scenario (f) empty autogenerate diff -- 039 (+ Task-3 model deletion) leaves models ⇔ DB consistent
# --------------------------------------------------------------------------------------------------

# 039 DROPS the files.state column + ix_files_state index; the models drop them too (Task 3). The scoped
# empty-diff check asserts autogenerate against the post-039 DB introduces NO add/remove of these two
# objects (identical scoping to test_migration_038). The forensic files_state_archive table is a
# migration-managed non-ORM artifact excluded from autogenerate (env.py include_object), so it is out of
# scope here too.
_O39_TABLES: set[str] = set()
_O39_INDEXES: set[str] = {"ix_files_state"}
_O39_COLUMNS: set[tuple[str, str]] = {("files", "state")}


def _diffs_touching_039(sync_conn: Connection) -> list[tuple[str, str]]:
    """Return the autogenerate diff ops that would create/drop a 039-scoped object (empty-diff scope)."""
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
        if (op_name in ("add_table", "remove_table") and getattr(diff[1], "name", None) in _O39_TABLES) or (
            op_name in ("add_index", "remove_index") and getattr(diff[1], "name", None) in _O39_INDEXES
        ):
            offenders.append((op_name, diff[1].name))
        elif op_name in ("add_column", "remove_column") and (diff[2], diff[3].name) in _O39_COLUMNS:
            offenders.append((op_name, f"{diff[2]}.{diff[3].name}"))
    return offenders


@pytest.mark.asyncio
async def test_039_autogenerate_diff_is_empty_for_dropped_objects() -> None:
    """Scenario (f): after 039 + the Task-3 model deletion, autogenerate re-adds neither files.state nor ix_files_state."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = await _chain_to_038(cfg)
    try:
        await asyncio.to_thread(upgrade_to, cfg, "039")
        async with engine.connect() as conn:
            offenders = await conn.run_sync(_diffs_touching_039)
        assert offenders == [], f"039 must leave models ⇔ DB consistent for files.state / ix_files_state: {offenders}"
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
