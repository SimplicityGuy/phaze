"""Durable invariants of the single ``039`` baseline migration (Phase 102 flatten, MIG-03).

The 001-039 per-migration test files died with the chain; this suite preserves their
durable value against the one baseline that replaced it:

* structural contract: ``revision == "039"``, ``down_revision is None`` (prod-at-039 no-op);
* the two seed tables are populated by a bare ``upgrade head`` (a schema-only baseline
  would be a broken fresh install);
* the 033 ``analysis_completed_at`` / ``failed_at`` NAND CHECK still rejects mixed rows;
* the varchar-enum CHECKs, partial/unique indexes, generated tsvector columns + GIN
  search, and the full table inventory survived the flatten;
* ``upgrade`` from empty + ``downgrade base`` round-trips cleanly;
* the ORM<->schema ``--autogenerate`` drift equals the FROZEN pre-flatten set -- the
  flatten's fidelity gate proved the chain and the baseline carry this exact same
  drift (ORM-less ``files_state_archive``, generated ``search_vector`` columns,
  trgm/partial/functional indexes, timestamp-typing nuances), so ANY change to the set
  (new drift, or silently resolved drift) fails and forces a deliberate update here.
  phaze-0jpe.4's migration 046 dropped ``fingerprint_results`` (+ its two partial/unique
  indexes) and narrowed ``stage_skip``'s CHECK, resolving the three PENDING-MIGRATION
  drift entries that used to sit here -- see migration 046's module docstring.
* phaze-0r9a: the seed INSERTs render bound values (not ``NULL``) in OFFLINE (``--sql``)
  mode too -- this one test needs no live database, since offline mode never connects.

Runs on the 5433 migrations harness (``MIGRATIONS_TEST_DATABASE_URL``, conftest.py) except
the offline-mode seed test, which is connection-free by construction.
"""

import asyncio
import contextlib
import importlib.util
import io
from pathlib import Path
import types
import uuid

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
import phaze.models  # noqa: F401  -- registers every table on Base.metadata for the autogenerate diff
from phaze.models.base import Base

from .conftest import (
    MIGRATIONS_TEST_DATABASE_URL,
    _build_alembic_config,
    _patched_settings_database_url,
    _reset_schema,
    downgrade_to,
    upgrade_to,
)


_BASELINE_PATH = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "039_baseline_schema.py"

_EXPECTED_TABLES = frozenset(
    {
        "agents",
        "analysis",
        "analysis_window",
        "cloud_job",
        "dedup_resolution",
        "discogs_links",
        "execution_log",
        "file_companions",
        "files",
        "files_state_archive",
        "metadata",
        "pipeline_stage_control",
        "proposals",
        "route_control",
        "scan_batches",
        "scheduling_ledger",
        "stage_skip",
        "tag_write_log",
        "tracklist_lookup_cache",
        "tracklist_tracks",
        "tracklist_versions",
        "tracklists",
    }
)

_EXPECTED_ENUM_CHECKS = frozenset(
    {
        "ck_agents_kind_enum",
        "ck_cloud_job_status_enum",
        "ck_cloud_job_cloud_phase_enum",
        "ck_stage_skip_enrich_only",
    }
)

# Partial (WHERE-qualified) indexes the chain accreted; representative durable set.
_EXPECTED_PARTIAL_INDEXES = frozenset(
    {
        "ix_agents_token_hash_active",
        "ix_analysis_completed",
        "ix_analysis_failed",
        "ix_cloud_job_awaiting",
        "ix_metadata_failed",
        "uq_proposals_file_id_pending",
        "uq_scan_batches_agent_id_live",
        "ix_discogs_links_one_accepted_per_track",
        "uq_scan_batches_agent_id_scan_path_running",
    }
)

_EXPECTED_GIN_INDEXES = frozenset(
    {
        "ix_files_search_vector",
        "ix_files_filename_trgm",
        "ix_metadata_search_vector",
        "ix_metadata_artist_trgm",
        "ix_tracklists_search_vector",
        "ix_tracklists_artist_trgm",
        "ix_discogs_links_fts",
    }
)

# The FROZEN ORM<->schema autogenerate drift (see module docstring). Proven identical
# between the pre-flatten chain and the baseline at flatten time (Phase 102 VERIFICATION).
_FROZEN_AUTOGEN_DRIFT = frozenset(
    {
        ("add_index", "ix_analysis_window_file_id"),
        ("modify_nullable", "discogs_links.created_at"),
        ("modify_nullable", "discogs_links.updated_at"),
        # phaze-36rc / migration 040: tag_write_log timestamps are now timestamptz in the DB, so the
        # naive ORM columns (plain DateTime / TimestampMixin) drift on TYPE -- exactly mirroring
        # execution_log below (naive model vs aware DB). These were formerly modify_nullable entries;
        # once the type also diverges, alembic groups both diffs and _canonical_diff keys them as
        # modify_type (the nullable delta is still present within the same grouped op).
        ("modify_type", "tag_write_log.created_at"),
        ("modify_type", "tag_write_log.updated_at"),
        ("modify_type", "tag_write_log.written_at"),
        ("modify_type", "agents.created_at"),
        ("modify_type", "agents.updated_at"),
        ("modify_type", "analysis.created_at"),
        ("modify_type", "analysis.updated_at"),
        ("modify_type", "execution_log.created_at"),
        ("modify_type", "execution_log.executed_at"),
        ("modify_type", "execution_log.updated_at"),
        ("modify_type", "file_companions.created_at"),
        ("modify_type", "file_companions.updated_at"),
        ("modify_type", "files.created_at"),
        ("modify_type", "files.updated_at"),
        ("modify_type", "metadata.created_at"),
        ("modify_type", "metadata.updated_at"),
        ("modify_type", "proposals.created_at"),
        ("modify_type", "proposals.updated_at"),
        ("modify_type", "scan_batches.created_at"),
        ("modify_type", "scan_batches.updated_at"),
        # phaze-fq9h.3 / migration 049: tracklist_lookup_cache's TimestampMixin columns are
        # timestamptz in the DB (following 040's tag_write_log precedent -- the drain records
        # attempt times across restarts and a naive column there would be a real defect), while
        # TimestampMixin's `Mapped[datetime]` is naive. Same long-standing ORM-side gap every table
        # above carries; the new table inherits it rather than introducing a new kind of drift.
        ("modify_type", "tracklist_lookup_cache.created_at"),
        ("modify_type", "tracklist_lookup_cache.updated_at"),
        ("remove_column", "files.search_vector"),
        ("remove_column", "metadata.search_vector"),
        ("remove_column", "tracklists.search_vector"),
        ("remove_index", "ix_agents_token_hash_active"),
        ("remove_index", "ix_analysis_window_bpm_fine"),
        ("remove_index", "ix_analysis_window_dance_coarse"),
        ("remove_index", "ix_analysis_window_file_tier_idx"),
        ("remove_index", "ix_analysis_window_mood"),
        ("remove_index", "ix_analysis_window_style"),
        ("remove_index", "ix_discogs_links_fts"),
        ("remove_index", "ix_execution_log_proposal_id"),
        ("remove_index", "ix_execution_log_status"),
        ("remove_index", "ix_files_filename_trgm"),
        ("remove_index", "ix_files_search_vector"),
        ("remove_index", "ix_metadata_artist_trgm"),
        ("remove_index", "ix_metadata_search_vector"),
        ("remove_index", "ix_tracklists_artist_trgm"),
        ("remove_index", "ix_tracklists_search_vector"),
        ("remove_table", "files_state_archive"),
    }
)


def _load_baseline_module() -> types.ModuleType:
    """Load ``039_baseline_schema.py`` by path (a digit-leading name can't be a plain import)."""
    spec = importlib.util.spec_from_file_location("baseline_schema_039", _BASELINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_diff(diff: object) -> tuple[str, str]:
    """Reduce an autogenerate diff entry to a stable ``(kind, object)`` key."""
    if isinstance(diff, list):  # modify_* ops arrive as [(kind, schema, table, column, ...)]
        kind, _schema, table, column = diff[0][0], diff[0][1], diff[0][2], diff[0][3]
        return (kind, f"{table}.{column}")
    assert isinstance(diff, tuple)
    kind = diff[0]
    if kind in ("add_column", "remove_column"):
        return (kind, f"{diff[2]}.{diff[3].name}")
    return (kind, str(diff[1].name))


# --- Static contract (no DB required) ---


def test_baseline_revision_contract() -> None:
    """The baseline reuses revision id 039 with no parent (the prod no-op contract)."""
    module = _load_baseline_module()
    assert module.revision == "039"
    assert module.down_revision is None


def test_baseline_is_the_only_migration() -> None:
    """The prune pattern holds: only the 039 baseline plus deliberately-landed post-flatten revisions.

    040 (phaze-36rc) lands tag_write_log timestamptz; 041 (phaze-5vmt) lands the
    UNIQUE(tracklist_id, version_number) constraint; 042 (phaze-2jl1 / phaze-y0j0) lands the
    scheduling_ledger.redrive_attempt column; 043 (phaze-gl1k) lands the partial
    UNIQUE(track_id) WHERE status='accepted' on discogs_links; 044 (phaze-1a71) lands the partial
    UNIQUE(agent_id, scan_path) WHERE status='running' on scan_batches; 045 (phaze-x4ux) lands the
    nullable files.original_filename_repaired mojibake-repair column; 046 (phaze-0jpe.4) drops
    fingerprint_results and narrows stage_skip's CHECK; 047 (phaze-s7mb) drops the never-populated
    analysis.fingerprint column; 048 (phaze-bto9) adds the files (original_filename, id) btree the
    tag-write review keyset paging orders and ranges on; 049 (phaze-fq9h.3) adds the
    tracklist_lookup_cache table that stops the rate-capped 1001TL drain re-asking questions it has
    already paid for. Any other resurrected 0xx chain file is a regression.
    """
    chain_files = sorted(p.name for p in _BASELINE_PATH.parent.glob("0*.py"))
    assert chain_files == [
        "039_baseline_schema.py",
        "040_tag_write_log_timestamptz.py",
        "041_tracklist_version_unique.py",
        "042_scheduling_ledger_redrive_attempt.py",
        "043_discogs_link_one_accepted_per_track.py",
        "044_scan_batches_no_duplicate_running.py",
        "045_files_original_filename_repaired.py",
        "046_drop_fingerprint_schema.py",
        "047_drop_analysis_fingerprint_column.py",
        "048_files_original_filename_id_btree.py",
        "049_tracklist_lookup_cache.py",
    ], f"unexpected chain files resurrected: {chain_files}"


def test_baseline_seed_inserts_render_bound_params_in_offline_sql_mode() -> None:
    """phaze-0r9a: ``alembic upgrade head --sql`` must not drop the seed INSERTs' bound values.

    Offline (``--sql``) mode runs against Alembic's ``MockConnection``, which compiles with
    ``literal_binds=True`` and silently discards a *separate* parameters argument -- the
    pre-fix ``bind.execute(sa.text(...), {"stage": stage})`` shape rendered every seed
    INSERT as ``VALUES (NULL, ...)``, a NOT NULL / primary-key violation on both
    ``pipeline_stage_control`` and ``route_control`` with `alembic` exiting 0 and no
    warning at generation time. Offline mode never opens a connection (that is the whole
    point of it), so this needs no live database -- it must NOT depend on the
    ``migrated_engine``/``MIGRATIONS_TEST_DATABASE_URL`` harness being up.
    """
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), _patched_settings_database_url(MIGRATIONS_TEST_DATABASE_URL):
        command.upgrade(cfg, "039", sql=True)
    sql = buffer.getvalue()
    for expected in ("VALUES ('metadata'", "VALUES ('analyze'", "VALUES ('fingerprint'", "VALUES ('global'"):
        assert expected in sql, f"offline-mode SQL is missing a bound seed value ({expected!r}); got:\n{sql}"
    unbound_marker = "VALUES (NULL"
    assert unbound_marker not in sql, f"offline-mode SQL rendered an unbound seed INSERT ({unbound_marker!r}):\n{sql}"


# --- Schema invariants (baseline-built DB via migrated_engine) ---


@pytest.mark.asyncio
async def test_alembic_version_is_head(migrated_engine: AsyncEngine) -> None:
    """A bare ``upgrade head`` on an empty DB lands at the current head (048: tag-write review btree)."""
    async with migrated_engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert version == "049"


@pytest.mark.asyncio
async def test_tracklist_version_unique_constraint_enforced(migrated_engine: AsyncEngine) -> None:
    """041 adds UNIQUE(tracklist_id, version_number): a duplicate version row is rejected (phaze-5vmt)."""
    tracklist_id = uuid.uuid4()
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tracklists (id, external_id, source_url, auto_linked, source, status, created_at, updated_at) "
                "VALUES (:id, :ext, :url, false, '1001tracklists', 'approved', NOW(), NOW())"
            ),
            {"id": tracklist_id, "ext": f"race-{tracklist_id}", "url": "https://example.com/tl"},
        )
        await conn.execute(
            text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 1, NOW())"),
            {"id": uuid.uuid4(), "tid": tracklist_id},
        )
    with pytest.raises(IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 1, NOW())"),
                {"id": uuid.uuid4(), "tid": tracklist_id},
            )


@pytest.mark.asyncio
async def test_discogs_link_one_accepted_per_track_enforced(migrated_engine: AsyncEngine) -> None:
    """043 adds a partial UNIQUE(track_id) WHERE status='accepted': a second accepted link for the
    same track is rejected at the DB level (phaze-gl1k, D-07 defense-in-depth)."""
    tracklist_id, version_id, track_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tracklists (id, external_id, source_url, auto_linked, source, status, created_at, updated_at) "
                "VALUES (:id, :ext, :url, false, '1001tracklists', 'approved', NOW(), NOW())"
            ),
            {"id": tracklist_id, "ext": f"d07-{tracklist_id}", "url": "https://example.com/tl"},
        )
        await conn.execute(
            text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 1, NOW())"),
            {"id": version_id, "tid": tracklist_id},
        )
        await conn.execute(
            text("INSERT INTO tracklist_tracks (id, version_id, position) VALUES (:id, :vid, 1)"),
            {"id": track_id, "vid": version_id},
        )
        await conn.execute(
            text("INSERT INTO discogs_links (id, track_id, discogs_release_id, confidence, status) VALUES (:id, :tid, 'r-1', 90.0, 'accepted')"),
            {"id": uuid.uuid4(), "tid": track_id},
        )
    with pytest.raises(IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO discogs_links (id, track_id, discogs_release_id, confidence, status) VALUES (:id, :tid, 'r-2', 80.0, 'accepted')"),
                {"id": uuid.uuid4(), "tid": track_id},
            )


@pytest.mark.asyncio
async def test_scan_batches_no_duplicate_running_enforced(migrated_engine: AsyncEngine) -> None:
    """044 adds a partial UNIQUE(agent_id, scan_path) WHERE status='running': a second RUNNING
    batch for the same agent+path is rejected at the DB level (phaze-1a71 durable guard)."""
    agent_id, scan_path = "baseline-scan-agent", "/data/music"
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files) "
                "VALUES (:id, :agent_id, :scan_path, 'running', 0, 0)"
            ),
            {"id": uuid.uuid4(), "agent_id": agent_id, "scan_path": scan_path},
        )
    with pytest.raises(IntegrityError):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files) "
                    "VALUES (:id, :agent_id, :scan_path, 'running', 0, 0)"
                ),
                {"id": uuid.uuid4(), "agent_id": agent_id, "scan_path": scan_path},
            )
    # A second COMPLETED batch for the same agent+path is fine -- the guard is RUNNING-scoped only.
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO scan_batches (id, agent_id, scan_path, status, total_files, processed_files) "
                "VALUES (:id, :agent_id, :scan_path, 'completed', 5, 5)"
            ),
            {"id": uuid.uuid4(), "agent_id": agent_id, "scan_path": scan_path},
        )


@pytest.mark.asyncio
async def test_tag_write_log_timestamps_are_timezone_aware(migrated_engine: AsyncEngine) -> None:
    """phaze-36rc / migration 040: tag_write_log timestamps are timestamptz, matching execution_log.

    Divergent typing (execution_log.executed_at timestamptz vs tag_write_log.written_at naive) made
    asyncpg decode one aware and one naive, 500-ing GET /record/{id} when both histories were present.
    After 040 every tag_write_log timestamp -- and execution_log.executed_at -- reports 'timestamp with
    time zone', so the driver decodes them uniformly aware and the merge-sort can never mix tz-awareness.
    """
    async with migrated_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND ((table_name = 'tag_write_log' AND column_name IN ('written_at', 'created_at', 'updated_at')) "
                    "OR (table_name = 'execution_log' AND column_name = 'executed_at'))"
                )
            )
        ).all()
    by_col = {(t, c): dtype for t, c, dtype in rows}
    assert by_col[("tag_write_log", "written_at")] == "timestamp with time zone"
    assert by_col[("tag_write_log", "created_at")] == "timestamp with time zone"
    assert by_col[("tag_write_log", "updated_at")] == "timestamp with time zone"
    # The reference sibling was already aware; both sides now agree.
    assert by_col[("execution_log", "executed_at")] == "timestamp with time zone"


@pytest.mark.asyncio
async def test_expected_tables_present(migrated_engine: AsyncEngine) -> None:
    """``upgrade head`` produces the full table inventory: the 039 baseline plus every later migration.

    ``tracklist_lookup_cache`` is migration 049's additive table (phaze-fq9h.3), not part of the
    baseline -- this fixture upgrades to HEAD, so the inventory is cumulative.
    """
    async with migrated_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all()
    tables = set(rows) - {"alembic_version"}
    assert tables == set(_EXPECTED_TABLES)


@pytest.mark.asyncio
async def test_seed_rows_present(migrated_engine: AsyncEngine) -> None:
    """020's pipeline_stage_control seed (minus 046's dropped 'fingerprint' row) + 031's route_control
    singleton survive the flatten."""
    async with migrated_engine.connect() as conn:
        stages = (await conn.execute(text("SELECT stage, paused, priority FROM pipeline_stage_control ORDER BY stage"))).all()
        route = (await conn.execute(text("SELECT id, force_local FROM route_control"))).all()
    assert [(s, p, prio) for s, p, prio in stages] == [("analyze", False, 50), ("metadata", False, 50)]
    assert route == [("global", False)]


@pytest.mark.asyncio
async def test_033_nand_check_rejects_mixed_row(migrated_engine: AsyncEngine) -> None:
    """The 033 CHECK still forbids a row that is both completed AND failed (FAIL-01/D-06)."""
    agent_id, file_id = "baseline-test-agent", uuid.uuid4()
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                "VALUES (:id, 'baseline-hash', '/x/a.mp3', 'a.mp3', '/x/a.mp3', 'mp3', 1, :agent_id)"
            ),
            {"id": file_id, "agent_id": agent_id},
        )
        # completed-only is fine ...
        await conn.execute(
            text("INSERT INTO analysis (id, file_id, analysis_completed_at) VALUES (:id, :file_id, NOW())"),
            {"id": uuid.uuid4(), "file_id": file_id},
        )
    # ... completed AND failed is rejected by ck_analysis_analysis_completed_xor_failed.
    with pytest.raises(IntegrityError, match="ck_analysis_analysis_completed_xor_failed"):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO analysis (id, file_id, analysis_completed_at, failed_at) VALUES (:id, :file_id, NOW(), NOW())"),
                {"id": uuid.uuid4(), "file_id": file_id},
            )


@pytest.mark.asyncio
async def test_enum_checks_present_and_enforced(migrated_engine: AsyncEngine) -> None:
    """The four varchar-enum CHECKs exist and still reject out-of-set values."""
    async with migrated_engine.connect() as conn:
        names = (await conn.execute(text("SELECT conname FROM pg_constraint WHERE contype = 'c'"))).scalars().all()
    assert set(names) >= _EXPECTED_ENUM_CHECKS
    with pytest.raises(IntegrityError, match="ck_agents_kind_enum"):
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES ('bogus-kind', 'bogus-kind', 'toaster', NOW(), NOW())"),
            )


@pytest.mark.asyncio
async def test_partial_and_gin_indexes_present(migrated_engine: AsyncEngine) -> None:
    """The partial (WHERE-qualified) and GIN index inventory survived the flatten."""
    async with migrated_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"))).all()
    defs = dict(rows)
    missing_partial = {n for n in _EXPECTED_PARTIAL_INDEXES if n not in defs or " WHERE " not in defs[n]}
    assert not missing_partial, f"partial indexes missing or unqualified: {missing_partial}"
    missing_gin = {n for n in _EXPECTED_GIN_INDEXES if n not in defs or "USING gin" not in defs[n]}
    assert not missing_gin, f"GIN indexes missing or wrong method: {missing_gin}"


@pytest.mark.asyncio
async def test_search_vector_generates_and_matches(migrated_engine: AsyncEngine) -> None:
    """``files.search_vector`` is a working generated tsvector column (009's FTS survives)."""
    agent_id, file_id = "baseline-fts-agent", uuid.uuid4()
    async with migrated_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"),
            {"id": agent_id},
        )
        await conn.execute(
            text(
                "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                "VALUES (:id, 'baseline-fts-hash', '/x/coachella_set.mp3', 'coachella_set.mp3', '/x/coachella_set.mp3', 'mp3', 1, :agent_id)"
            ),
            {"id": file_id, "agent_id": agent_id},
        )
    async with migrated_engine.connect() as conn:
        hit = (
            await conn.execute(
                text("SELECT id FROM files WHERE search_vector @@ plainto_tsquery('simple', 'coachella')"),
            )
        ).scalar_one()
    assert hit == file_id


@pytest.mark.asyncio
async def test_autogenerate_drift_is_frozen(migrated_engine: AsyncEngine) -> None:
    """ORM<->schema drift equals the frozen pre-flatten set: no NEW drift, no silent resolution."""

    def _diff_sync(conn: Connection) -> frozenset[tuple[str, str]]:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": False})
        return frozenset(_canonical_diff(d) for d in compare_metadata(ctx, Base.metadata))

    async with migrated_engine.connect() as conn:
        drift = await conn.run_sync(_diff_sync)
    unexpected = drift - _FROZEN_AUTOGEN_DRIFT
    resolved = _FROZEN_AUTOGEN_DRIFT - drift
    assert not unexpected, f"NEW ORM<->schema drift (add a migration or update the ORM): {sorted(unexpected)}"
    assert not resolved, f"drift silently resolved (adjust _FROZEN_AUTOGEN_DRIFT deliberately): {sorted(resolved)}"


# --- Round-trip (drives its own upgrade/downgrade; no fixture) ---


@pytest.mark.asyncio
async def test_upgrade_downgrade_roundtrip() -> None:
    """Empty -> upgrade head -> downgrade base -> empty -> upgrade head, all clean."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = None
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        await asyncio.to_thread(upgrade_to, cfg, "head")
        await asyncio.to_thread(downgrade_to, cfg, "base")
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        async with engine.connect() as conn:
            leftover = (
                (
                    await conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename != 'alembic_version'"),
                    )
                )
                .scalars()
                .all()
            )
        assert leftover == [], f"downgrade base left tables behind: {leftover}"
        await asyncio.to_thread(upgrade_to, cfg, "head")
        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        assert version == "049"
    finally:
        if engine is not None:
            await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


@pytest.mark.asyncio
async def test_migration_041_dedupes_preexisting_duplicate_versions() -> None:
    """041's upgrade must renumber pre-existing (tracklist_id, version_number) duplicates BEFORE
    creating the UNIQUE constraint, else upgrade aborts with UniqueViolation on any database the
    pre-fix concurrent-scrape race (phaze-5vmt) actually hit (phaze-am5p).

    Seeds two duplicate groups on 040 (pre-constraint), directly against the table -- the shape
    the ORM-serialized race would have left behind -- then upgrades to 041 and asserts:
    * the upgrade completes without raising (the regression this bead fixes);
    * no duplicate (tracklist_id, version_number) pair remains;
    * every original row (and its dependent tracklist_tracks row) survives, renumbered rather
      than deleted -- fk_tracklist_tracks_version_id_tracklist_versions has no ON DELETE CASCADE;
    * the winner of a group is the row referenced by tracklists.latest_version_id when present,
      else the lowest id -- both fallback paths are exercised.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = None
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        await asyncio.to_thread(upgrade_to, cfg, "040")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        tracklist_id = uuid.uuid4()
        winner_id, loser_id = uuid.uuid4(), uuid.uuid4()
        # No row in this second group matches latest_version_id, so the winner must fall back to
        # the lowest id between the two.
        fallback_a, fallback_b = uuid.uuid4(), uuid.uuid4()
        fallback_winner_id = min(fallback_a, fallback_b)

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tracklists (id, external_id, source_url, auto_linked, source, status, latest_version_id, created_at, updated_at) "
                    "VALUES (:id, :ext, :url, false, '1001tracklists', 'approved', :latest, NOW(), NOW())"
                ),
                {"id": tracklist_id, "ext": f"dedupe-{tracklist_id}", "url": "https://example.com/tl", "latest": winner_id},
            )
            # Group 1 (version_number=2): winner_id is referenced by latest_version_id.
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 2, NOW())"),
                {"id": winner_id, "tid": tracklist_id},
            )
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 2, NOW())"),
                {"id": loser_id, "tid": tracklist_id},
            )
            # A track row hanging off the loser -- must survive the dedupe untouched.
            await conn.execute(
                text("INSERT INTO tracklist_tracks (id, version_id, position) VALUES (:id, :vid, 1)"),
                {"id": uuid.uuid4(), "vid": loser_id},
            )
            # Group 2 (version_number=5): neither row is latest_version_id -- fallback to min(id).
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 5, NOW())"),
                {"id": fallback_a, "tid": tracklist_id},
            )
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 5, NOW())"),
                {"id": fallback_b, "tid": tracklist_id},
            )
        await engine.dispose()
        engine = None

        # The regression: this must not raise UniqueViolation.
        await asyncio.to_thread(upgrade_to, cfg, "041")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id, version_number FROM tracklist_versions WHERE tracklist_id = :tid"),
                    {"tid": tracklist_id},
                )
            ).all()
            dupes = (
                await conn.execute(
                    text("SELECT version_number FROM tracklist_versions WHERE tracklist_id = :tid GROUP BY version_number HAVING COUNT(*) > 1"),
                    {"tid": tracklist_id},
                )
            ).all()
            loser_track_count = (
                await conn.execute(text("SELECT COUNT(*) FROM tracklist_tracks WHERE version_id = :vid"), {"vid": loser_id})
            ).scalar_one()

        by_id = {row.id: row.version_number for row in rows}
        assert dupes == [], "upgrade to 041 must leave no duplicate (tracklist_id, version_number) rows"
        assert len(rows) == 4, "dedupe must renumber, not delete -- all four original version rows survive"
        assert by_id[winner_id] == 2, "the row referenced by tracklists.latest_version_id keeps its version_number"
        assert by_id[fallback_winner_id] == 5, "with no latest_version_id match in the group, the lowest id keeps its version_number"
        assert by_id[loser_id] != 2, "the loser is renumbered off the colliding version_number"
        assert loser_track_count == 1, "the loser's tracklist_tracks row must survive untouched (no ON DELETE CASCADE)"
    finally:
        if engine is not None:
            await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


@pytest.mark.asyncio
async def test_migration_046_drops_fingerprint_results_and_narrows_stage_skip_check() -> None:
    """046 (phaze-0jpe.4): upgrade discards fingerprint_results (with any existing rows), deletes
    stray ``stage='fingerprint'`` rows from ``stage_skip``/``pipeline_stage_control`` before/while
    narrowing the enforced stage sets, and downgrade restores the schema (not the discarded data).

    Seeds a pre-046 database (upgraded only to 045) with one row in each of the three affected
    tables/constraints, then drives upgrade -> 046 and asserts every fingerprint-only fragment is
    gone and the surviving rows/constraints are untouched. Then downgrades back to 045 and asserts
    the schema (table + indexes + FK + seed row + widened CHECK) is restored -- the module docstring
    on migration 046 is explicit that only the DATA is unrecoverable, not the schema.
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine

    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = None
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        await asyncio.to_thread(upgrade_to, cfg, "045")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        agent_id, file_id = "mig046-agent", uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES (:id, :id, 'fileserver', NOW(), NOW())"),
                {"id": agent_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                    "VALUES (:id, 'mig046-hash', '/x/b.mp3', 'b.mp3', '/x/b.mp3', 'mp3', 1, :agent_id)"
                ),
                {"id": file_id, "agent_id": agent_id},
            )
            # A pre-existing fingerprint_results row -- must be discarded by the DROP TABLE.
            await conn.execute(
                text(
                    "INSERT INTO fingerprint_results (id, file_id, engine, status, created_at, updated_at) "
                    "VALUES (:id, :file_id, 'audfprint', 'success', NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "file_id": file_id},
            )
            # A pre-existing stage='fingerprint' force-skip row -- must be deleted before the CHECK narrows.
            await conn.execute(
                text(
                    "INSERT INTO stage_skip (id, file_id, stage, reason, skipped_at, created_at, updated_at) "
                    "VALUES (:id, :file_id, 'fingerprint', 'pre-046 residual', NOW(), NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "file_id": file_id},
            )
            # A surviving metadata skip row -- must NOT be touched by 046.
            await conn.execute(
                text(
                    "INSERT INTO stage_skip (id, file_id, stage, reason, skipped_at, created_at, updated_at) "
                    "VALUES (:id, :file_id, 'metadata', 'unrelated skip', NOW(), NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "file_id": file_id},
            )
        await engine.dispose()
        engine = None

        # The regression this bead's acceptance covers: upgrade must not raise on a database that
        # actually has the residual rows the narrowed CHECK would otherwise reject.
        await asyncio.to_thread(upgrade_to, cfg, "046")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        async with engine.connect() as conn:
            tables = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all()
            stage_control_stages = (await conn.execute(text("SELECT stage FROM pipeline_stage_control ORDER BY stage"))).scalars().all()
            skip_rows = (await conn.execute(text("SELECT stage FROM stage_skip ORDER BY stage"))).scalars().all()
        assert "fingerprint_results" not in tables, "046 must drop fingerprint_results"
        assert stage_control_stages == ["analyze", "metadata"], "046 must delete the seeded 'fingerprint' pipeline_stage_control row"
        assert skip_rows == ["metadata"], "046 must delete stray stage='fingerprint' stage_skip rows and leave others untouched"

        # The narrowed CHECK now rejects a fresh stage='fingerprint' insert.
        with pytest.raises(IntegrityError, match="ck_stage_skip_enrich_only"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO stage_skip (id, file_id, stage, reason, skipped_at, created_at, updated_at) "
                        "VALUES (:id, :file_id, 'fingerprint', 'post-046 write attempt', NOW(), NOW(), NOW())"
                    ),
                    {"id": uuid.uuid4(), "file_id": file_id},
                )
        await engine.dispose()
        engine = None

        # Downgrade restores the schema (table + indexes + FK + seed row + widened CHECK) -- not
        # the discarded data, per migration 046's module docstring.
        await asyncio.to_thread(downgrade_to, cfg, "045")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        async with engine.connect() as conn:
            tables = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all()
            stage_control_stages = (await conn.execute(text("SELECT stage FROM pipeline_stage_control ORDER BY stage"))).scalars().all()
            fprint_row_count = (await conn.execute(text("SELECT COUNT(*) FROM fingerprint_results"))).scalar_one()
        assert "fingerprint_results" in tables, "downgrade must recreate fingerprint_results"
        assert stage_control_stages == ["analyze", "fingerprint", "metadata"], "downgrade must restore the 'fingerprint' seed row"
        assert fprint_row_count == 0, "downgrade restores schema only -- the discarded row data is gone for good"

        # The widened CHECK accepts 'fingerprint' again post-downgrade.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO stage_skip (id, file_id, stage, reason, skipped_at, created_at, updated_at) "
                    "VALUES (:id, :file_id, 'fingerprint', 'post-downgrade write', NOW(), NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "file_id": file_id},
            )
    finally:
        if engine is not None:
            await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    """Strip the ``+asyncpg`` driver qualifier so the raw ``asyncpg`` client can connect."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://")


@pytest.mark.asyncio
async def test_migration_048_upgrade_heals_invalid_leftover_index() -> None:
    """phaze-44sj: an INTERRUPTED ``CREATE INDEX CONCURRENTLY`` leaves ``ix_files_original_filename_id``
    present but INVALID (``pg_index.indisvalid = false``); re-running ``upgrade`` to 048 must detect
    that and drop+rebuild rather than silently no-op via ``IF NOT EXISTS``.

    Simulates the interrupted build with the same mechanism a real interruption produces: an open
    REPEATABLE READ transaction against ``files`` holds a snapshot old enough that Postgres's
    CONCURRENTLY machinery must wait on it before marking the new index valid, giving a second
    connection running the concurrent build a window to be cancelled mid-build via
    ``pg_cancel_backend`` -- exactly what a killed connection, a ``statement_timeout``, or a deploy
    restart does to a live CONCURRENTLY build. Then asserts the pre-fix precondition (index present,
    INVALID) actually holds before driving ``upgrade`` to 048 and asserting it heals.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    dsn = _asyncpg_dsn(MIGRATIONS_TEST_DATABASE_URL)
    blocker_conn: asyncpg.Connection | None = None
    cic_conn: asyncpg.Connection | None = None
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        await asyncio.to_thread(upgrade_to, cfg, "047")

        # 1) Hold a REPEATABLE READ snapshot open against `files` so the CONCURRENTLY build below
        #    must block waiting for it, giving us a window to cancel the build mid-flight.
        blocker_conn = await asyncpg.connect(dsn)
        blocker_tx = blocker_conn.transaction(isolation="repeatable_read")
        await blocker_tx.start()
        await blocker_conn.fetch("SELECT 1 FROM files")

        # 2) Start the concurrent build on its own connection; it will hang behind the blocker.
        cic_conn = await asyncpg.connect(dsn)
        cic_task = asyncio.create_task(
            cic_conn.execute("CREATE INDEX CONCURRENTLY ix_files_original_filename_id ON public.files USING btree (original_filename, id)")
        )

        # 3) Poll pg_stat_activity for the build's backend pid, then cancel it mid-build --
        #    the same signal a killed connection or statement_timeout delivers in production.
        cancel_conn = await asyncpg.connect(dsn)
        try:
            pid = None
            for _ in range(100):
                # datname scope: pg_stat_activity is CLUSTER-wide, and without it this poll can
                # match another worktree's copy of the same build on the shared harness -- and
                # then pg_cancel_backend THEIR build (tests/shared/test_cluster_wide_catalog_scoping.py).
                row = await cancel_conn.fetchrow(
                    "SELECT pid FROM pg_stat_activity "
                    "WHERE query ILIKE '%CREATE INDEX CONCURRENTLY%ix_files_original_filename_id%' "
                    "AND pid != pg_backend_pid() AND datname = current_database()"
                )
                if row is not None:
                    pid = row["pid"]
                    break
                await asyncio.sleep(0.05)
            assert pid is not None, "the CONCURRENTLY build never showed up in pg_stat_activity to cancel"
            await cancel_conn.execute("SELECT pg_cancel_backend($1)", pid)
        finally:
            await cancel_conn.close()

        with pytest.raises(asyncpg.PostgresError):
            await cic_task

        # Release the blocker -- nothing else should stay stuck on its snapshot.
        await blocker_tx.rollback()
        await blocker_conn.close()
        blocker_conn = None
        await cic_conn.close()
        cic_conn = None

        # Confirm the simulated precondition this bead fixes: an INVALID index was left behind.
        verify_conn = await asyncpg.connect(dsn)
        try:
            precondition = await verify_conn.fetchrow(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = 'public.ix_files_original_filename_id'::regclass"
            )
        finally:
            await verify_conn.close()
        assert precondition is not None, "simulated interruption did not leave an index behind"
        assert precondition["indisvalid"] is False, "simulated interruption did not leave the index INVALID"

        # The regression this bead fixes: upgrade must self-heal, not silently no-op.
        await asyncio.to_thread(upgrade_to, cfg, "048")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        try:
            async with engine.connect() as conn:
                healed = (
                    await conn.execute(text("SELECT indisvalid FROM pg_index WHERE indexrelid = 'public.ix_files_original_filename_id'::regclass"))
                ).scalar_one()
            assert healed is True, "upgrade must drop and rebuild the INVALID leftover index rather than no-op on it"
        finally:
            await engine.dispose()
    finally:
        if cic_conn is not None:
            await cic_conn.close()
        if blocker_conn is not None:
            await blocker_conn.close()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
