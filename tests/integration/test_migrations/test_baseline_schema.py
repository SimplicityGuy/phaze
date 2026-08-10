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
  trgm/partial/functional indexes), so ANY change to the set (new drift, or silently
  resolved drift) fails and forces a deliberate update here.
  phaze-0jpe.4's migration 046 dropped ``fingerprint_results`` (+ its two partial/unique
  indexes) and narrowed ``stage_skip``'s CHECK, resolving the three PENDING-MIGRATION
  drift entries that used to sit here -- see migration 046's module docstring.
  phaze-cz3m's migration 049 resolved the twenty timestamp ``modify_type`` entries by
  making the schema uniformly ``timestamptz``; the "timestamp-typing nuances" this
  docstring used to list as accepted drift were a live defect, not a nuance.
  phaze-x8tof: alembic 1.19.0 (the 2026.8.1 dependency refresh) added a CHECK-constraint
  comparator this gate had never had, which surfaced five constraints the
  ``ck_%(table_name)s_%(constraint_name)s`` convention had DOUBLE-prefixed in the database
  (``ck_agents_ck_agents_id_charset`` and friends -- two of them dating to the 039 baseline
  and migration 053, months before the release). Migration 056 renames them to what the ORM
  renders, so the frozen set still carries no check-constraint entry and must not gain one:
  a ``ck``-shaped add/remove pair means the double-prefix is back, and
  ``test_no_check_constraint_is_double_prefixed`` should be failing alongside it.
* phaze-cz3m: no timestamp column in the schema is naive, and no mapped ``DateTime``
  column declares itself naive -- the two halves of the same invariant, asserted
  schema-wide so a new table cannot reintroduce the split.
* phaze-0r9a: the seed INSERTs render bound values (not ``NULL``) in OFFLINE (``--sql``)
  mode too -- this one test needs no live database, since offline mode never connects.

Runs on the 5433 migrations harness (``MIGRATIONS_TEST_DATABASE_URL``, conftest.py) except
the offline-mode seed test, which is connection-free by construction.
"""

import asyncio
import contextlib
from datetime import datetime
import importlib.util
import io
from pathlib import Path
import types
import uuid

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import asyncpg
import pytest
from sqlalchemy import DateTime, text
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
        # phaze-2mwyo (migration 055): the DURABLE per-file cloud budget. Deliberately its OWN table
        # rather than columns on `files` -- the D-14 reaper deletes the `cloud_job` sidecar that used to
        # hold the budget, and Phase 90 (MIG-04) removed `files.state` precisely so `files` carries
        # description, never scheduling state.
        "cloud_budget",
        "cloud_job",
        "dedup_resolution",
        "discogs_links",
        "execution_log",
        "file_companions",
        "filename_convention",
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
        "tracklist_priority_flags",
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
        # phaze-cz3m / migration 049 DELETED the twenty ("modify_type", "<table>.created_at|updated_at")
        # entries that used to sit here. Every one was the same defect: the ORM declared a naive
        # DateTime (TimestampMixin, or a bare `DateTime`) against a column the 039 baseline had made
        # `timestamptz`. That was never harmless typing noise -- it is what took the cloud staging
        # scheduler down for ~10 h, because SQLAlchemy emits a `::TIMESTAMP WITHOUT TIME ZONE` cast for
        # any bind param typed off such a column and asyncpg cannot encode an aware value through it.
        # 049 made the whole schema `timestamptz` and TimestampMixin now declares timezone=True, so the
        # two sides agree and there is no type drift left to freeze. Do NOT re-add entries of this
        # shape: a `modify_type` on a timestamp column means the split is back, and
        # test_every_schema_timestamp_column_is_timezone_aware /
        # test_every_model_datetime_column_declares_timezone_aware should be failing alongside it.
        #
        # The three tag_write_log entries below are what remained once the type agreed. They are a
        # NULLABILITY delta, not a tz one -- the 039 baseline created these columns without NOT NULL
        # while the ORM declares them non-nullable, exactly like discogs_links above. Previously
        # alembic grouped that delta into the same op as the type diff and `_canonical_diff` keyed the
        # pair as `modify_type`, hiding it; removing the type diff surfaced it unchanged. Pre-existing
        # and out of scope for phaze-cz3m.
        ("modify_nullable", "tag_write_log.created_at"),
        ("modify_nullable", "tag_write_log.updated_at"),
        ("modify_nullable", "tag_write_log.written_at"),
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
    tag-write review keyset paging orders and ranges on; 049 (phaze-cz3m) makes every schema
    timestamp timestamptz; 050 (phaze-fq9h.3) creates tracklist_lookup_cache; 051 (phaze-fq9h.7)
    adds tracklists propagation columns + narrows external_id uniqueness to canonical rows; 052
    (phaze-fq9h.8) creates tracklist_priority_flags, the persisted home for an operator's "answer
    this file first"; 053 (phaze-5fta.2) creates filename_convention, the generic corpus-learned
    convention store keyed (scope, scope_value, convention_kind) with a DB-derived confidence
    column; 054 (phaze-1q4g) adds cloud_job.node_loss_redrives, the independent budget for re-drives
    caused by the pod dying with its node (which must not spend the file's analyze `attempts`, but
    must still be bounded); 055 (phaze-2mwyo) creates cloud_budget, the DURABLE per-file cloud budget
    that outlives the `cloud_job` sidecar the D-14 reaper deletes -- the row 054's per-chain counters
    were being erased with, which let one file start an unbounded number of fresh attempt chains; 056
    (phaze-x8tof) renames the five CHECK constraints the `ck_%(table_name)s_%(constraint_name)s`
    convention had double-prefixed in the database; 057 (phaze-mwbz3) adds
    cloud_job.node_loss_pending, the durable carry for a node-loss verdict classified while the
    still-terminating re-drive deferral is waiting -- without it the verdict died with the deferral's
    stack frame and a Job that vanished before the next tick silently charged `attempts` instead of
    the tighter `node_loss_redrives` ceiling.
    Any other resurrected 0xx chain file is a regression.
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
        "049_all_timestamps_timestamptz.py",
        "050_tracklist_lookup_cache.py",
        "051_tracklists_propagation.py",
        "052_tracklist_priority_flags.py",
        "053_filename_convention.py",
        "054_cloud_job_node_loss_redrives.py",
        "055_cloud_budget_ledger.py",
        "056_fix_double_prefixed_check_constraints.py",
        "057_cloud_job_node_loss_pending.py",
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
    """A bare ``upgrade head`` on an empty DB lands at the current head (057: the node-loss verdict carry)."""
    async with migrated_engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert version == "057"


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
async def test_every_schema_timestamp_column_is_timezone_aware(migrated_engine: AsyncEngine) -> None:
    """phaze-cz3m / migration 049: NO naive timestamp column survives anywhere in the schema.

    Deliberately schema-WIDE rather than a list of known tables. Its predecessor
    (``test_tag_write_log_timestamps_are_timezone_aware``, phaze-36rc / migration 040) asserted the
    three columns that migration had just fixed, so it stayed green through the outage that the
    remaining 24 naive columns went on to cause -- a guard that only watches the last fire cannot
    catch the next one. Phrased as "the set of naive timestamp columns is empty", it fails on any
    NEW table that reintroduces one, without anybody remembering to extend a list.

    The 039 baseline created these columns inconsistently -- 10 tables aware, 11 naive, all from the
    same ``TimestampMixin``. asyncpg decodes by column OID, so the tz-awareness of a value depended on
    which table it came from, and mixing the two raises ``TypeError``/``DataError`` deep inside the
    driver rather than anywhere near the bug. Migration 049 removed the distinction entirely.
    """
    async with migrated_engine.connect() as conn:
        naive = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type = 'timestamp without time zone' "
                    "ORDER BY table_name, column_name"
                )
            )
        ).all()
    assert [f"{t}.{c}" for t, c in naive] == [], (
        "naive timestamp columns found -- every timestamp column must be 'timestamp with time zone' "
        "(see migration 049 / models.base.TimestampMixin). Add an ALTER ... AT TIME ZONE 'UTC' migration."
    )


def test_every_model_datetime_column_declares_timezone_aware() -> None:
    """The ORM half of the same invariant: no mapped DateTime column may claim to be naive.

    The database being uniformly ``timestamptz`` is only half the fix. SQLAlchemy's asyncpg dialect
    emits an explicit ``$n::TIMESTAMP WITHOUT TIME ZONE`` cast for any bind param typed off a column
    declared naive, so a model that lies about an aware column still breaks on the WRITE path even
    though every read works -- which is exactly how the cloud-staging keyset cursor failed while
    every other query against ``files`` stayed green. Needs no database: it reads the mapped metadata.
    """
    naive = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]
    assert naive == [], (
        f"model DateTime columns declared naive: {naive} -- every mapped datetime column must be "
        "DateTime(timezone=True) to match the schema (see models.base.TimestampMixin, phaze-cz3m)."
    )


@pytest.mark.asyncio
async def test_expected_tables_present(migrated_engine: AsyncEngine) -> None:
    """The baseline creates the full 23-table inventory the chain produced."""
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
async def test_no_check_constraint_is_double_prefixed(migrated_engine: AsyncEngine) -> None:
    """phaze-x8tof: no CHECK constraint carries its ``ck_<table>_`` prefix twice.

    ``models/base.py``'s convention keys ``ck`` on ``ck_%(table_name)s_%(constraint_name)s`` -- the only
    entry interpolating ``%(constraint_name)s``, which is exactly the token that makes SQLAlchemy apply
    the convention to EXPLICITLY NAMED constraints too. So a ``CheckConstraint(..., name="ck_<table>_x")``
    (in a model OR in a migration -- alembic's ``op.*`` builds against the same ``naming_convention``)
    lands in Postgres as ``ck_<table>_ck_<table>_x``. Five constraints shipped that way before migration
    056 repaired them; ``filename_convention``'s doubled name was already 66 bytes and got truncated to a
    hash-suffixed stub (``..._counts_no_e237``), so this is a live collision hazard, not cosmetics.

    ``test_autogenerate_drift_is_frozen`` also catches this (alembic 1.19 added the CHECK comparator that
    made it visible in the first place), but only as an opaque add/remove pair. This assertion names the
    defect, so the next occurrence is diagnosable from the failure line alone.
    """
    async with migrated_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT t.relname AS table_name, c.conname AS constraint_name
                      FROM pg_constraint c
                      JOIN pg_class t ON t.oid = c.conrelid
                      JOIN pg_namespace n ON n.oid = t.relnamespace
                     WHERE n.nspname = 'public' AND c.contype = 'c'
                    """
                )
            )
        ).all()
    doubled = sorted(f"{table}.{name}" for table, name in rows if name.startswith(f"ck_{table}_ck_{table}_"))
    assert not doubled, f"CHECK constraints double-prefixed by the ck naming convention (declare BARE names): {doubled}"


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
        assert version == "057"
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


async def _set_database_timezone(database_url: str, timezone: str | None) -> None:
    """``ALTER DATABASE ... SET TimeZone`` (or RESET when ``timezone`` is None), affecting NEW sessions.

    Lets a test drive a migration under a session TimeZone other than the harness container's UTC,
    which is the only way to tell a correct ``USING <col> AT TIME ZONE 'UTC'`` conversion apart from
    a naive one. ALTER DATABASE cannot run inside a transaction block, hence AUTOCOMMIT.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    database = database_url.rsplit("/", 1)[-1].split("?")[0]
    # The database name reaches DDL as an identifier, which is not parameterizable. It comes from
    # this suite's own env var, never from user input, and is pinned to the harness naming scheme
    # before use so a malformed URL fails here rather than composing DDL.
    assert database.replace("_", "").isalnum(), f"refusing to build DDL from unexpected database name {database!r}"
    action = "RESET TimeZone" if timezone is None else f"SET TimeZone TO '{timezone}'"
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'ALTER DATABASE "{database}" {action}'))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_049_preserves_existing_timestamp_values_as_utc() -> None:
    """049 must REINTERPRET naive timestamps as UTC, never shift them (phaze-cz3m).

    The failure mode this guards is silent and unrecoverable: a bare
    ``ALTER COLUMN ... TYPE timestamptz`` with no ``USING`` clause resolves naive values against the
    SESSION TimeZone, so on a non-UTC server every historical row would slide by the offset and
    nothing would look broken afterwards. The migration pins the interpretation with an explicit
    ``USING <col> AT TIME ZONE 'UTC'``; this test is what makes that clause non-negotiable.

    Seeds a pre-049 database (upgraded only to 048, where the columns are still naive) with a known
    wall-clock value, upgrades, and asserts the stored instant reads back as that same wall clock in
    UTC. Uses ``cloud_job`` -- the table whose keyset cursor caused the outage -- plus ``tracklists``
    so both a converted-in-049 audit table and a converted-in-049 content table are covered.

    Runs the upgrade under a deliberately NON-UTC database TimeZone. This is the whole point: the
    test harness container runs UTC, where a missing ``USING`` clause produces byte-identical
    results and the test would pass vacuously. Forcing ``America/Los_Angeles`` for the duration of
    the upgrade makes the two spellings diverge by the 7-8 h offset, so the assertions below can
    actually fail. Verified to fail (7 h / 25200 s drift on both columns) against a USING-less
    variant of 049 before being committed.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = None
    # A deliberately non-midnight, sub-second value: a whole-hour offset shift would still be
    # visible, but so would a fractional-second truncation. The text form is the assertion target
    # and the naive datetime is what gets bound -- derived from it so there is one source of truth.
    seeded = "2026-06-14 02:57:18.490794"
    seeded_dt = datetime.fromisoformat(seeded)
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        await asyncio.to_thread(upgrade_to, cfg, "048")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        agent_id, file_id, tracklist_id = f"tz-{uuid.uuid4().hex[:8]}", uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            # Confirm the precondition rather than assume it: if 048 ever stops leaving these naive,
            # this test is no longer testing a conversion and should fail loudly here.
            naive_before = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = 'public' "
                        "AND data_type = 'timestamp without time zone' AND "
                        "((table_name = 'cloud_job' AND column_name = 'created_at') OR (table_name = 'tracklists' AND column_name = 'created_at'))"
                    )
                )
            ).scalar_one()
            assert naive_before == 2, "precondition: cloud_job.created_at and tracklists.created_at are naive at 048"

            await conn.execute(text("INSERT INTO agents (id, name, scan_roots) VALUES (:id, :id, '[]'::jsonb)"), {"id": agent_id})
            await conn.execute(
                text(
                    "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                    "VALUES (:id, :h, '/x/t.mp3', 't.mp3', '/x/t.mp3', 'mp3', 1, :agent_id)"
                ),
                {"id": file_id, "h": f"tzhash-{file_id}", "agent_id": agent_id},
            )
            await conn.execute(
                text("INSERT INTO cloud_job (id, file_id, status, created_at, updated_at) VALUES (:id, :fid, 'awaiting', :ts, :ts)"),
                {"id": uuid.uuid4(), "fid": file_id, "ts": seeded_dt},
            )
            await conn.execute(
                text(
                    "INSERT INTO tracklists (id, external_id, source_url, auto_linked, source, status, created_at, updated_at) "
                    "VALUES (:id, :ext, 'https://example.com/tl', false, '1001tracklists', 'approved', :ts, :ts)"
                ),
                {"id": tracklist_id, "ext": f"tz-{tracklist_id}", "ts": seeded_dt},
            )
        await engine.dispose()
        engine = None

        # Force a non-UTC TimeZone for the sessions alembic is about to open, so a USING-less
        # ALTER would resolve the naive values against LA rather than UTC and shift them.
        # ALTER DATABASE affects NEW sessions, which is exactly what upgrade_to opens.
        await _set_database_timezone(MIGRATIONS_TEST_DATABASE_URL, "America/Los_Angeles")
        try:
            await asyncio.to_thread(upgrade_to, cfg, "049")
        finally:
            await _set_database_timezone(MIGRATIONS_TEST_DATABASE_URL, None)

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        async with engine.connect() as conn:
            # Read back the wall clock AT UTC. Equal to the seeded literal iff the value was
            # reinterpreted as UTC rather than shifted by the server's offset.
            cloud_utc = (
                await conn.execute(text("SELECT (created_at AT TIME ZONE 'UTC')::text FROM cloud_job WHERE file_id = :fid"), {"fid": file_id})
            ).scalar_one()
            tracklist_utc = (
                await conn.execute(text("SELECT (created_at AT TIME ZONE 'UTC')::text FROM tracklists WHERE id = :id"), {"id": tracklist_id})
            ).scalar_one()
            offset_seconds = (
                await conn.execute(
                    text("SELECT EXTRACT(EPOCH FROM (created_at - CAST(:ts AS timestamp) AT TIME ZONE 'UTC')) FROM cloud_job WHERE file_id = :fid"),
                    {"ts": seeded_dt, "fid": file_id},
                )
            ).scalar_one()

        assert cloud_utc == seeded, f"049 shifted cloud_job.created_at: seeded {seeded!r}, read back {cloud_utc!r} at UTC"
        assert tracklist_utc == seeded, f"049 shifted tracklists.created_at: seeded {seeded!r}, read back {tracklist_utc!r} at UTC"
        assert offset_seconds == 0, f"049 must reinterpret naive values as UTC, not shift them (drifted {offset_seconds}s)"
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


@pytest.mark.asyncio
async def test_migration_051_downgrade_deletes_discogs_links_before_tracklist_tracks() -> None:
    """051's downgrade must clear ``discogs_links`` for propagated rows before deleting
    ``tracklist_tracks``, or it aborts with a ForeignKeyViolation on any database where Discogs
    matching ran over a propagated tracklist (phaze-psa96).

    Seeds a propagated tracklist (``propagated_from_set_key`` set) with a version, a track, and a
    ``discogs_links`` row against that track -- the exact state ``POST /pipeline/match-tracklists``
    produces once it starts including propagated rows -- then downgrades to 050 and asserts:
    * the downgrade completes without raising (the regression this bead fixes);
    * the propagated tracklist/version/track/discogs_links rows are all gone;
    * a co-existing CANONICAL tracklist (and its own discogs_links row) survives untouched.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = None
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        await asyncio.to_thread(upgrade_to, cfg, "051")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        canonical_id, canonical_version_id, canonical_track_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        propagated_id, propagated_version_id, propagated_track_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            # A canonical (non-propagated) tracklist with its own accepted discogs_links row --
            # must survive the downgrade untouched.
            await conn.execute(
                text(
                    "INSERT INTO tracklists (id, external_id, source_url, auto_linked, source, status, created_at, updated_at) "
                    "VALUES (:id, :ext, :url, false, '1001tracklists', 'approved', NOW(), NOW())"
                ),
                {"id": canonical_id, "ext": f"psa96-canon-{canonical_id}", "url": "https://example.com/tl-canon"},
            )
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 1, NOW())"),
                {"id": canonical_version_id, "tid": canonical_id},
            )
            await conn.execute(
                text("INSERT INTO tracklist_tracks (id, version_id, position) VALUES (:id, :vid, 1)"),
                {"id": canonical_track_id, "vid": canonical_version_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO discogs_links (id, track_id, discogs_release_id, confidence, status) VALUES (:id, :tid, 'r-canon', 90.0, 'accepted')"
                ),
                {"id": uuid.uuid4(), "tid": canonical_track_id},
            )

            # A propagated tracklist -- the exact chain get_match_pending_tracklists surfaces to
            # POST /pipeline/match-tracklists once discogs matching has run over it.
            await conn.execute(
                text(
                    "INSERT INTO tracklists (id, external_id, source_url, auto_linked, source, status, "
                    "propagated_from_set_key, propagation_confidence, created_at, updated_at) "
                    "VALUES (:id, :ext, :url, false, '1001tracklists', 'approved', :set_key, 'exact', NOW(), NOW())"
                ),
                {"id": propagated_id, "ext": f"psa96-canon-{canonical_id}", "url": "https://example.com/tl-prop", "set_key": "set-psa96"},
            )
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, scraped_at) VALUES (:id, :tid, 1, NOW())"),
                {"id": propagated_version_id, "tid": propagated_id},
            )
            await conn.execute(
                text("INSERT INTO tracklist_tracks (id, version_id, position) VALUES (:id, :vid, 1)"),
                {"id": propagated_track_id, "vid": propagated_version_id},
            )
            # The discogs_links row against the PROPAGATED track -- without deleting this first,
            # downgrade's DELETE FROM tracklist_tracks violates fk_discogs_links_track_id_tracklist_tracks.
            await conn.execute(
                text(
                    "INSERT INTO discogs_links (id, track_id, discogs_release_id, confidence, status) VALUES (:id, :tid, 'r-prop', 85.0, 'candidate')"
                ),
                {"id": uuid.uuid4(), "tid": propagated_track_id},
            )
        await engine.dispose()
        engine = None

        # The regression this bead fixes: downgrade must not raise ForeignKeyViolation.
        await asyncio.to_thread(downgrade_to, cfg, "050")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        async with engine.connect() as conn:
            remaining_tracklists = (await conn.execute(text("SELECT id FROM tracklists"))).scalars().all()
            remaining_tracks = (await conn.execute(text("SELECT id FROM tracklist_tracks"))).scalars().all()
            remaining_links = (await conn.execute(text("SELECT track_id FROM discogs_links"))).scalars().all()
        assert remaining_tracklists == [canonical_id], "downgrade must delete only the propagated tracklist"
        assert remaining_tracks == [canonical_track_id], "downgrade must delete the propagated tracklist_tracks row too"
        assert remaining_links == [canonical_track_id], "downgrade must delete discogs_links for the propagated track, keeping the canonical one"
    finally:
        if engine is not None:
            await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


@pytest.mark.asyncio
async def test_migration_042_downgrade_backfills_redrive_attempt_into_payload() -> None:
    """042's downgrade must mirror-write ``redrive_attempt`` back into the legacy payload JSONB
    keys before dropping the column, or a rollback silently resets every in-flight push/upload
    re-drive budget to zero (phaze-dt2cx).

    Seeds two post-042 ``scheduling_ledger`` rows (one ``push_file``, one ``s3_upload``) with a
    non-zero ``redrive_attempt`` and a payload that carries no legacy counter key (the realistic
    post-042 shape, since nothing writes the legacy keys anymore), then downgrades to 041 and
    asserts the payload now carries the counter under the function-appropriate legacy key --
    exactly what pre-042 code reads.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    engine = None
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        await asyncio.to_thread(upgrade_to, cfg, "042")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        push_key, s3_key = "dt2cx-push-key", "dt2cx-s3-key"
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO scheduling_ledger (key, function, routing, payload, redrive_attempt, enqueued_at, created_at, updated_at) "
                    "VALUES (:key, 'push_file', 'agent', '{}'::jsonb, 4, NOW(), NOW(), NOW())"
                ),
                {"key": push_key},
            )
            await conn.execute(
                text(
                    "INSERT INTO scheduling_ledger (key, function, routing, payload, redrive_attempt, enqueued_at, created_at, updated_at) "
                    "VALUES (:key, 's3_upload', 'agent', '{}'::jsonb, 2, NOW(), NOW(), NOW())"
                ),
                {"key": s3_key},
            )
        await engine.dispose()
        engine = None

        # The regression this bead fixes: the counter must not be silently discarded.
        await asyncio.to_thread(downgrade_to, cfg, "041")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        async with engine.connect() as conn:
            push_payload = (await conn.execute(text("SELECT payload FROM scheduling_ledger WHERE key = :key"), {"key": push_key})).scalar_one()
            s3_payload = (await conn.execute(text("SELECT payload FROM scheduling_ledger WHERE key = :key"), {"key": s3_key})).scalar_one()
        assert push_payload.get("push_attempt") == 4, "downgrade must back-fill push_file's counter into payload.push_attempt"
        assert s3_payload.get("s3_upload_attempt") == 2, "downgrade must back-fill s3_upload's counter into payload.s3_upload_attempt"
    finally:
        if engine is not None:
            await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
