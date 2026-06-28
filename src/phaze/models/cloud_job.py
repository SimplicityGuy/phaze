"""CloudJob model -- per-``file_id`` sidecar for the S3 object-staging leg (Phase 53, D-03).

One row per file (unique FK to ``files.id`` -- one active cloud burst per file) tracking the
ephemeral S3 staging object: its ``s3_key`` (file_id-scoped), the stage ``status`` (a
DB-checked enum), and the multipart ``upload_id`` so the control plane can complete/abort the
upload. The control plane presigns the PUT/GET and deletes the object (KSTAGE-01..04); this
row is the durable record of where the bytes live and what stage the burst is in.

D-03 keeps this table staging-only NOW: ``kueue_workload`` (the Kueue Job name, Phase 54) and
``cloud_phase`` (Phase 55) are added in their OWN migrations so each migration stays scoped to
its phase. Mirrors the v5.0 ``scheduling_ledger`` per-file sidecar precedent.

The ``status`` column is a string-backed :class:`CloudJobStatus` StrEnum (FileState precedent):
new members need no enum migration, only the CHECK-constraint membership list. ``created_at`` /
``updated_at`` come from :class:`TimestampMixin` -- do not redeclare them here.
"""

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class CloudJobStatus(enum.StrEnum):
    """Stage of the S3 staging upload for a file (string-backed; FileState precedent).

    String-backed so future members (e.g. a download/verify stage) need only the CHECK
    membership list updated, not a Postgres enum-type migration. The DB CHECK constraint
    (``ck_cloud_job_status_enum``) is the authoritative membership gate.
    """

    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"


class CloudJob(TimestampMixin, Base):
    """One row per file_id tracking its ephemeral S3 staging object (Phase 53, D-03)."""

    __tablename__ = "cloud_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Unique FK to files.id: one active cloud burst per file (metadata.py precedent).
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    # file_id-scoped staging key in the bucket (KSTAGE-04).
    s3_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # String-backed CloudJobStatus; the DB CHECK below is the membership gate.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Multipart upload id so the control plane can complete/abort the upload (D-01); NULL until
    # the multipart upload is initiated.
    upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'uploaded', 'failed')",
            name="status_enum",
        ),
    )
