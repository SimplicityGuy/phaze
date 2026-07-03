"""Tests for TagWriteLog model and TagWriteStatus enum."""

import uuid

from phaze.models.base import Base
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus


class TestTagWriteStatus:
    """Tests for TagWriteStatus enum."""

    def test_completed_value(self) -> None:
        assert TagWriteStatus.COMPLETED == "completed"

    def test_failed_value(self) -> None:
        assert TagWriteStatus.FAILED == "failed"

    def test_discrepancy_value(self) -> None:
        assert TagWriteStatus.DISCREPANCY == "discrepancy"

    def test_is_str_enum(self) -> None:
        assert isinstance(TagWriteStatus.COMPLETED, str)


class TestTagWriteLog:
    """Tests for TagWriteLog model."""

    def test_table_name(self) -> None:
        assert TagWriteLog.__tablename__ == "tag_write_log"

    def test_table_in_metadata(self) -> None:
        assert "tag_write_log" in Base.metadata.tables

    def test_required_columns(self) -> None:
        columns = {c.name for c in TagWriteLog.__table__.columns}
        required = {
            "id",
            "file_id",
            "before_tags",
            "after_tags",
            "source",
            "status",
            "discrepancies",
            "error_message",
            "written_at",
            "created_at",
            "updated_at",
        }
        assert required.issubset(columns)

    def test_id_is_primary_key(self) -> None:
        pk_cols = [c.name for c in TagWriteLog.__table__.primary_key.columns]
        assert pk_cols == ["id"]

    def test_file_id_has_foreign_key(self) -> None:
        col = TagWriteLog.__table__.c.file_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "files.id" in fk_targets

    def test_indexes_exist(self) -> None:
        index_names = {idx.name for idx in TagWriteLog.__table__.indexes}
        assert "ix_tag_write_log_file_id" in index_names
        assert "ix_tag_write_log_status" in index_names

    def test_model_instantiation(self) -> None:
        file_id = uuid.uuid4()
        log = TagWriteLog(
            file_id=file_id,
            before_tags={"artist": "Old Artist"},
            after_tags={"artist": "New Artist"},
            source="tracklist",
            status=TagWriteStatus.COMPLETED,
        )
        assert log.file_id == file_id
        assert log.before_tags == {"artist": "Old Artist"}
        assert log.after_tags == {"artist": "New Artist"}
        assert log.source == "tracklist"
        assert log.status == TagWriteStatus.COMPLETED
        assert log.discrepancies is None
        assert log.error_message is None
