"""Tests for SQLAlchemy model definitions."""

from phaze.models import AnalysisResult, ExecutionLog, FileMetadata, FileRecord, RenameProposal
from phaze.models.base import Base


def test_all_tables_defined() -> None:
    """All expected tables should be defined in metadata."""
    table_names = set(Base.metadata.tables.keys())
    expected = {
        "agents",
        "files",
        "metadata",
        "analysis",
        "analysis_window",
        "proposals",
        "execution_log",
        "scan_batches",
        "file_companions",
        "tracklists",
        "tracklist_versions",
        "tracklist_tracks",
        "discogs_links",
        "tag_write_log",
        "pipeline_stage_control",
        "scheduling_ledger",
        "cloud_job",
        # phaze-2mwyo (migration 055): the DURABLE per-file cloud budget ledger. Separate from cloud_job
        # because `routers/agent_analysis`'s D-14 reaper deletes that sidecar on every analyze terminal,
        # taking the file's retry budget with it -- which let one file start an unbounded number of
        # fresh cloud attempt chains.
        "cloud_budget",
        "route_control",  # Phase 71 (71-02, BEUI-02): force-local control row (migration 031)
        "dedup_resolution",  # Phase 77 (77-02, D-07): dedup marker sidecar (migration 032)
        "stage_skip",  # Phase 87 (87-01, D-13): force-skip marker sidecar (migration 037)
        # phaze-fq9h.3 (migration 049): persisted positive/negative 1001TL lookup cache, so the
        # rate-capped drain never re-spends a request on a set it has already asked about.
        "tracklist_lookup_cache",
        # phaze-fq9h.8 (migration 052): persisted operator "answer this file first" priority flag,
        # so it survives past the single drain job it was originally passed into.
        "tracklist_priority_flags",
        # phaze-5fta.2 (migration 053): generic corpus-learned convention store, keyed
        # (scope, scope_value, convention_kind) with a DB-derived confidence column.
        "filename_convention",
        # phaze-6nrrf (migration 059): the durable operator ARM/DISARM flag for the continuous
        # 1001Tracklists drain -- one singleton row, seeded disarmed (DEFAULT OFF).
        "tracklist_drain_arm_state",
    }
    assert expected == table_names


def test_file_record_columns() -> None:
    """FileRecord should have all required columns."""
    columns = {c.name for c in FileRecord.__table__.columns}
    required = {
        "id",
        "sha256_hash",
        "original_path",
        "original_filename",
        "current_path",
        "file_type",
        "file_size",
        "created_at",
        "updated_at",
    }
    assert required.issubset(columns)


def test_metadata_has_jsonb_column() -> None:
    """FileMetadata should have a JSONB raw_tags column."""
    col = FileMetadata.__table__.columns["raw_tags"]
    assert "JSONB" in str(col.type)


def test_analysis_has_jsonb_column() -> None:
    """AnalysisResult should have a JSONB features column."""
    col = AnalysisResult.__table__.columns["features"]
    assert "JSONB" in str(col.type)


def test_proposal_has_status_column() -> None:
    """RenameProposal should have a status column with a default value."""
    col = RenameProposal.__table__.columns["status"]
    assert col.default is not None


def test_execution_log_has_sha256_verified() -> None:
    """ExecutionLog should have a boolean sha256_verified column."""
    col = ExecutionLog.__table__.columns["sha256_verified"]
    assert "BOOLEAN" in str(col.type).upper()


def test_execution_log_tablename() -> None:
    """ExecutionLog should use execution_log as the table name."""
    assert ExecutionLog.__tablename__ == "execution_log"


def test_file_record_has_batch_id() -> None:
    """FileRecord should have an optional batch_id column with ForeignKey to scan_batches."""
    col = FileRecord.__table__.columns["batch_id"]
    assert col.nullable is True
    assert len(col.foreign_keys) == 1
    fk = next(iter(col.foreign_keys))
    assert fk.target_fullname == "scan_batches.id"


async def test_tables_created_in_database(async_engine) -> None:  # type: ignore[no-untyped-def]
    """Verify all tables can be created in a real PostgreSQL database."""
    from sqlalchemy import inspect

    async with async_engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    expected = {"files", "metadata", "analysis", "proposals", "execution_log", "scan_batches", "file_companions"}
    assert expected.issubset(set(table_names))
