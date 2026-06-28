"""Add the ``cloud_job`` per-file_id staging sidecar table (Phase 53, D-03).

Additive-only, reversible migration. Creates the standalone ``cloud_job`` app table -- one row
per ``file_id`` (unique FK to ``files.id`` -- one active cloud burst per file) recording the
ephemeral S3 staging object for the object-staging leg: its file_id-scoped ``s3_key``, the
stage ``status`` (a DB-checked enum), and the multipart ``upload_id``.

D-03 keeps this STAGING-ONLY: ``kueue_workload`` (Phase 54) and ``cloud_phase`` (Phase 55) are
added in their OWN migrations so each migration stays scoped to its phase.

The ``status`` CHECK (declared name ``status_enum``, auto-prefixed to ``ck_cloud_job_status_enum``
by the naming convention) restricts the value to ``{'uploading', 'uploaded', 'failed'}`` at the
database -- the authoritative membership gate for the string-backed ``CloudJobStatus`` StrEnum.

CRITICAL: this migration touches ONLY ``cloud_job``.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions; an Alembic
migration touching it would collide -- 020 CRITICAL banner).

Revision ID: 025
Revises: 024
Create Date: 2026-06-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "025"
down_revision: str | Sequence[str] | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create cloud_job with a unique FK to files.id and the status CHECK enum."""
    op.create_table(
        "cloud_job",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("s3_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("upload_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cloud_job")),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_cloud_job_file_id_files")),
        sa.UniqueConstraint("file_id", name=op.f("uq_cloud_job_file_id")),
    )
    op.create_check_constraint("status_enum", "cloud_job", "status IN ('uploading', 'uploaded', 'failed')")


def downgrade() -> None:
    """Drop the status CHECK then the table (dependents first; mirror of upgrade).

    Pass the bare constraint name ``status_enum`` -- the ``ck_%(table_name)s_%(constraint_name)s``
    naming convention re-applies the ``ck_cloud_job_`` prefix, resolving to the live
    ``ck_cloud_job_status_enum`` (passing the already-prefixed name double-prefixes it).
    """
    op.drop_constraint("status_enum", "cloud_job", type_="check")
    op.drop_table("cloud_job")
