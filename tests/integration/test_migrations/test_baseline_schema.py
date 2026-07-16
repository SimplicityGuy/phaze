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
  42-entry drift (ORM-less ``files_state_archive``, generated ``search_vector`` columns,
  trgm/partial/functional indexes, timestamp-typing nuances), so ANY change to the set
  (new drift, or silently resolved drift) fails and forces a deliberate update here.

Runs on the 5433 migrations harness (``MIGRATIONS_TEST_DATABASE_URL``, conftest.py).
"""

import asyncio
import importlib.util
from pathlib import Path
import types
import uuid

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

import phaze.models  # noqa: F401  -- registers every table on Base.metadata for the autogenerate diff
from phaze.models.base import Base

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


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
        "fingerprint_results",
        "metadata",
        "pipeline_stage_control",
        "proposals",
        "route_control",
        "scan_batches",
        "scheduling_ledger",
        "stage_skip",
        "tag_write_log",
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
        "ix_fprint_success",
        "ix_metadata_failed",
        "uq_proposals_file_id_pending",
        "uq_scan_batches_agent_id_live",
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
        ("modify_nullable", "tag_write_log.created_at"),
        ("modify_nullable", "tag_write_log.updated_at"),
        ("modify_nullable", "tag_write_log.written_at"),
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
    """The prune pattern holds until a NEW revision intentionally lands: 039 has no siblings named 0xx."""
    chain_files = sorted(p.name for p in _BASELINE_PATH.parent.glob("0*.py"))
    assert chain_files == ["039_baseline_schema.py"], f"unexpected chain files resurrected: {chain_files}"


# --- Schema invariants (baseline-built DB via migrated_engine) ---


@pytest.mark.asyncio
async def test_alembic_version_is_039(migrated_engine: AsyncEngine) -> None:
    """A bare ``upgrade head`` on an empty DB lands exactly at 039."""
    async with migrated_engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert version == "039"


@pytest.mark.asyncio
async def test_expected_tables_present(migrated_engine: AsyncEngine) -> None:
    """The baseline creates the full 22-table inventory the chain produced."""
    async with migrated_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all()
    tables = set(rows) - {"alembic_version"}
    assert tables == set(_EXPECTED_TABLES)


@pytest.mark.asyncio
async def test_seed_rows_present(migrated_engine: AsyncEngine) -> None:
    """020's pipeline_stage_control seed + 031's route_control singleton survive the flatten."""
    async with migrated_engine.connect() as conn:
        stages = (await conn.execute(text("SELECT stage, paused, priority FROM pipeline_stage_control ORDER BY stage"))).all()
        route = (await conn.execute(text("SELECT id, force_local FROM route_control"))).all()
    assert [(s, p, prio) for s, p, prio in stages] == [("analyze", False, 50), ("fingerprint", False, 50), ("metadata", False, 50)]
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
        assert version == "039"
    finally:
        if engine is not None:
            await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
