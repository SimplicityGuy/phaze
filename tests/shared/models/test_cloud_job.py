"""Tests for the CloudJob model (Phase 53 Plan 01, Task 2; D-03).

``cloud_job`` is the per-``file_id`` staging sidecar for the S3 object-staging leg: one row
per file (unique FK -- one active burst per file), an ``s3_key`` (file_id-scoped), a stage
``status`` enum (DB-checked), and the multipart ``upload_id``. ``kueue_workload`` /
``cloud_phase`` are deferred to Phases 54/55.

Pure-schema tests run without a DB; the round-trip / unique-FK / CHECK-constraint tests use
the ``session`` fixture (real Postgres via ``Base.metadata.create_all``). The reversible
migration is covered in ``tests/test_migrations/test_migration_025_cloud_job.py``.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from phaze.models.base import Base
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState


def _make_file(*, file_type: str = "flac", state: str = FileState.AWAITING_CLOUD) -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
        state=state,
    )


class TestCloudJobSchema:
    """Table name / columns / PK / unique-FK shape -- no DB required."""

    def test_table_name(self) -> None:
        assert CloudJob.__tablename__ == "cloud_job"

    def test_table_in_metadata(self) -> None:
        assert "cloud_job" in Base.metadata.tables

    def test_required_columns(self) -> None:
        columns = {c.name for c in CloudJob.__table__.columns}
        required = {"id", "file_id", "s3_key", "status", "upload_id", "created_at", "updated_at"}
        assert required.issubset(columns)

    def test_kube_columns_present(self) -> None:
        # Phase 54 (D-09): kueue_workload / attempts / inadmissible land in migration 026.
        columns = {c.name for c in CloudJob.__table__.columns}
        assert {"kueue_workload", "attempts", "inadmissible"}.issubset(columns)

    def test_cloud_phase_nullable_string(self) -> None:
        # Phase 55 (D-04): cloud_phase is the k8s-only Kueue admission progression --
        # nullable String(20), NULL for a1/local rows. Supersedes the Phase 54 deferral guard.
        col = CloudJob.__table__.columns["cloud_phase"]
        assert col.nullable is True
        assert col.type.length == 20

    def test_kueue_workload_nullable_string(self) -> None:
        col = CloudJob.__table__.columns["kueue_workload"]
        assert col.nullable is True
        assert col.type.length == 255

    def test_attempts_non_null_default_zero(self) -> None:
        col = CloudJob.__table__.columns["attempts"]
        assert col.nullable is False
        assert col.default.arg == 0
        assert col.server_default.arg == "0"

    def test_inadmissible_non_null_default_false(self) -> None:
        col = CloudJob.__table__.columns["inadmissible"]
        assert col.nullable is False
        assert col.default.arg is False
        assert col.server_default.arg == "false"

    def test_id_is_primary_key(self) -> None:
        pk_cols = [c.name for c in CloudJob.__table__.primary_key.columns]
        assert pk_cols == ["id"]

    def test_file_id_is_unique_fk_to_files(self) -> None:
        col = CloudJob.__table__.columns["file_id"]
        assert col.unique is True
        assert col.nullable is False
        assert len(col.foreign_keys) == 1
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "files.id"

    def test_status_enum_members(self) -> None:
        assert CloudJobStatus.UPLOADING == "uploading"
        assert CloudJobStatus.UPLOADED == "uploaded"
        assert CloudJobStatus.FAILED == "failed"

    def test_status_enum_kube_lifecycle_members(self) -> None:
        # Phase 54 (D-09): submit/reconcile lifecycle members.
        assert CloudJobStatus.SUBMITTED == "submitted"
        assert CloudJobStatus.RUNNING == "running"
        assert CloudJobStatus.SUCCEEDED == "succeeded"


class TestCloudJobPersistence:
    """Round-trip + unique-FK + status CHECK against a real Postgres session."""

    async def test_round_trip(self, session) -> None:  # type: ignore[no-untyped-def]
        file = _make_file()
        session.add(file)
        await session.flush()

        job = CloudJob(file_id=file.id, s3_key=f"staging/{file.id}.flac", status=CloudJobStatus.UPLOADING, upload_id="mpu-123")
        session.add(job)
        await session.commit()
        await session.refresh(job)

        assert job.id is not None
        assert job.file_id == file.id
        assert job.s3_key == f"staging/{file.id}.flac"
        assert job.status == CloudJobStatus.UPLOADING
        assert job.upload_id == "mpu-123"
        assert job.created_at is not None

    async def test_upload_id_optional(self, session) -> None:  # type: ignore[no-untyped-def]
        file = _make_file()
        session.add(file)
        await session.flush()

        job = CloudJob(file_id=file.id, s3_key=f"staging/{file.id}.flac", status=CloudJobStatus.UPLOADED)
        session.add(job)
        await session.commit()
        await session.refresh(job)
        assert job.upload_id is None

    async def test_unique_file_id_violation(self, session) -> None:  # type: ignore[no-untyped-def]
        file = _make_file()
        session.add(file)
        await session.flush()

        session.add(CloudJob(file_id=file.id, s3_key="staging/a", status=CloudJobStatus.UPLOADING))
        session.add(CloudJob(file_id=file.id, s3_key="staging/b", status=CloudJobStatus.UPLOADING))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_status_check_constraint_rejects_bad_value(self, session) -> None:  # type: ignore[no-untyped-def]
        file = _make_file()
        session.add(file)
        await session.flush()

        session.add(CloudJob(file_id=file.id, s3_key="staging/x", status="bogus"))
        with pytest.raises(IntegrityError):
            await session.commit()
