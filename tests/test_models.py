"""Tests for SQLAlchemy model definitions."""

from phaze.models import AnalysisResult, ExecutionLog, FileMetadata, FileRecord, RenameProposal
from phaze.models.base import Base


def test_all_tables_defined() -> None:
    """All 5 expected tables should be defined in metadata."""
    table_names = set(Base.metadata.tables.keys())
    expected = {"files", "metadata", "analysis", "proposals", "execution_log"}
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
        "state",
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
    """FileRecord should have an optional batch_id column."""
    col = FileRecord.__table__.columns["batch_id"]
    assert col.nullable is True


async def test_tables_created_in_database(async_engine) -> None:  # type: ignore[no-untyped-def]
    """Verify all tables can be created in a real PostgreSQL database."""
    from sqlalchemy import inspect

    async with async_engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    expected = {"files", "metadata", "analysis", "proposals", "execution_log"}
    assert expected.issubset(set(table_names))
