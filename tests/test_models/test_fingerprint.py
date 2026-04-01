"""Tests for FingerprintResult model."""

import uuid

from phaze.models.fingerprint import FingerprintResult


class TestFingerprintResultModel:
    """Tests for the FingerprintResult ORM model."""

    def test_fingerprint_result_has_id(self):
        r = FingerprintResult(file_id=uuid.uuid4(), engine="audfprint", status="success")
        assert hasattr(r, "id")

    def test_fingerprint_result_with_success_status(self):
        fid = uuid.uuid4()
        r = FingerprintResult(file_id=fid, engine="audfprint", status="success")
        assert r.file_id == fid
        assert r.engine == "audfprint"
        assert r.status == "success"

    def test_fingerprint_result_with_failed_status_and_error(self):
        r = FingerprintResult(
            file_id=uuid.uuid4(),
            engine="panako",
            status="failed",
            error_message="Connection timeout",
        )
        assert r.status == "failed"
        assert r.error_message == "Connection timeout"

    def test_fingerprint_result_error_message_nullable(self):
        r = FingerprintResult(file_id=uuid.uuid4(), engine="audfprint", status="success")
        assert r.error_message is None

    def test_fingerprint_result_has_timestamp_columns(self):
        r = FingerprintResult(file_id=uuid.uuid4(), engine="audfprint", status="success")
        assert hasattr(r, "created_at")
        assert hasattr(r, "updated_at")

    def test_fingerprint_result_tablename(self):
        assert FingerprintResult.__tablename__ == "fingerprint_results"

    def test_fingerprint_result_file_id_fk_to_files(self):
        """Verify file_id FK references files.id."""
        col = FingerprintResult.__table__.columns["file_id"]
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "files.id" in fk_targets

    def test_fingerprint_result_unique_index_on_file_engine(self):
        """Verify unique index on (file_id, engine) exists."""
        indexes = FingerprintResult.__table__.indexes
        unique_idx = [idx for idx in indexes if idx.name == "ix_fprint_file_engine"]
        assert len(unique_idx) == 1
        assert unique_idx[0].unique is True
        col_names = {c.name for c in unique_idx[0].columns}
        assert col_names == {"file_id", "engine"}
