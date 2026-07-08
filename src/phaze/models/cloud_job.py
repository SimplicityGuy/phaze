"""CloudJob model -- per-``file_id`` sidecar for the S3 object-staging leg (Phase 53, D-03).

One row per file (unique FK to ``files.id`` -- one active cloud burst per file) tracking the
ephemeral S3 staging object: its ``s3_key`` (file_id-scoped), the stage ``status`` (a
DB-checked enum), and the multipart ``upload_id`` so the control plane can complete/abort the
upload. The control plane presigns the PUT/GET and deletes the object (KSTAGE-01..04); this
row is the durable record of where the bytes live and what stage the burst is in.

Phase 54 (D-09) extends this with the Kube submit/reconcile lifecycle: the ``kueue_workload``
(Job name), ``attempts`` (bounded re-drive counter) and ``inadmissible`` (operator-alert flag)
columns, plus the SUBMITTED/RUNNING/SUCCEEDED status members. ``cloud_phase`` (Phase 55) is the
only column still deferred to its OWN migration so each migration stays scoped to its phase.
Mirrors the v5.0 ``scheduling_ledger`` per-file sidecar precedent.

The ``status`` column is a string-backed :class:`CloudJobStatus` StrEnum (FileState precedent):
new members need no enum migration, only the CHECK-constraint membership list. ``created_at`` /
``updated_at`` come from :class:`TimestampMixin` -- do not redeclare them here.
"""

import enum
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, text
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
    # Phase 54 (D-09): submit/reconcile lifecycle members. The reconcile cron drives a
    # cloud_job row SUBMITTED -> RUNNING -> SUCCEEDED (or FAILED); only the CHECK membership
    # list changes (string-backed, no Postgres enum-type migration).
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    # Phase 77 (D-04): AWAITING_CLOUD sidecar representation. An awaiting file carries a cloud_job row
    # status='awaiting' (s3_key/upload_id NULL). String-backed -- only the CHECK membership list widens,
    # no Postgres enum-type migration. 'awaiting' is 8 chars, fits status String(16).
    AWAITING = "awaiting"


class CloudPhase(enum.StrEnum):
    """Kueue admission progression for a k8s cloud burst (Phase 55, D-04; string-backed).

    ORTHOGONAL to the ``inadmissible`` fault flag: ``cloud_phase`` tracks how far a Job has
    progressed through Kueue admission (quota wait -> admitted -> running -> finished), NOT
    whether the LocalQueue is misconfigured. NULL for a1/local rows (admission is k8s-only).
    The DB CHECK constraint (``ck_cloud_job_cloud_phase_enum``) is the authoritative membership
    gate; new members need only the CHECK list updated, not a Postgres enum-type migration.
    """

    QUEUED_BEHIND_QUOTA = "queued_behind_quota"
    ADMITTED = "admitted"
    RUNNING = "running"
    FINISHED = "finished"


class CloudJob(TimestampMixin, Base):
    """One row per file_id tracking its ephemeral S3 staging object (Phase 53, D-03)."""

    __tablename__ = "cloud_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Unique FK to files.id: one active cloud burst per file (metadata.py precedent).
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    # file_id-scoped staging key in the bucket (KSTAGE-04). Phase 68 (D-08): now nullable -- a compute
    # burst carries no S3 object (it rsync-pushes over Tailscale), so its cloud_job row leaves s3_key NULL;
    # Kueue/S3-staged rows still stamp it as before.
    s3_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # String-backed CloudJobStatus; the DB CHECK below is the membership gate.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Multipart upload id so the control plane can complete/abort the upload (D-01); NULL until
    # the multipart upload is initiated.
    upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Phase 54 (D-09): the Kueue/Job name stamped at submit; the reconcile cron looks the Job up
    # by this name. NULL until the file is submitted to Kube.
    kueue_workload: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Bounded re-drive counter (D-08): incremented on each Failed/Evicted re-submit; once it
    # exceeds cloud_submit_max_attempts the file is marked ANALYSIS_FAILED.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    # Drives the D-06 operator alert: set when the Kueue Workload is Inadmissible (never enters
    # the re-drive cap path).
    inadmissible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)
    # Phase 55 (D-04): the Kueue admission progression (queued_behind_quota -> admitted -> running ->
    # finished). NULL for a1/local rows (admission is k8s-only); kept ORTHOGONAL to ``inadmissible``.
    cloud_phase: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Phase 68 (D-06): config-derived backend registry id stamped at dispatch (per-backend in-flight
    # accounting substrate, BACK-02). NULLABLE with NO backfill -- the a1/k8s paths were never deployed
    # live so there are ~zero rows to migrate, and a migration cannot know a registry entry id; new rows
    # stamp it going forward. Plain free-text (no CHECK/enum).
    backend_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Phase 70 (D-01/D-06): records which ``BucketConfig.id`` staged the current object (MKUE-04).
    # NULLABLE with NO backfill -- the a1/k8s paths were never deployed live (~zero rows) and a
    # migration cannot know a per-file bucket choice; new rows stamp it going forward. Plain
    # free-text (no CHECK/enum). The recorded value is AUTHORITATIVE -- presign/cleanup READ it,
    # never re-derive. ``unique(file_id)`` (L72) is preserved -- cloud_job stays one-row-per-file (D-02).
    staging_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint(
            # Phase 77 (D-04): 'awaiting' added (7 -> incl awaiting members). Bare name "status_enum" --
            # the ck_%(table_name)s_%(constraint_name)s convention re-prefixes to ck_cloud_job_status_enum.
            "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed', 'awaiting')",
            name="status_enum",
        ),
        CheckConstraint(
            "cloud_phase IN ('queued_behind_quota', 'admitted', 'running', 'finished')",
            name="cloud_phase_enum",
        ),
        # Phase 77 (PERF-01, migration 032): awaiting-lookup partial index mirroring migration 032.
        Index("ix_cloud_job_awaiting", "file_id", postgresql_where=text("status = 'awaiting'")),
    )
