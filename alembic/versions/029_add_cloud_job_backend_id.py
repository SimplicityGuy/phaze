"""Add ``cloud_job.backend_id`` + make ``s3_key`` nullable (Phase 68, D-06/D-08).

Additive, reversible migration -- the per-backend in-flight accounting substrate (BACK-02). It
changes ``cloud_job`` in two behavior-preserving ways:

* Adds ``backend_id`` (``String(255)``, NULL) -- the config-derived backend registry id stamped at
  dispatch going forward. D-06: it is added NULLABLE with **NO backfill** -- the a1/k8s paths were
  never deployed live so there are ~zero rows to migrate, and a migration cannot reliably know a
  registry entry id. Plain free-text: **no CHECK/enum change** (unlike 026's status-CHECK swap).
* Makes ``s3_key`` nullable (D-08) -- a compute burst rsync-pushes over Tailscale and carries no S3
  object, so its ``cloud_job`` row legitimately leaves ``s3_key`` NULL. Kueue/S3-staged rows still
  stamp it. ``s3_key`` was ``NOT NULL`` only for the S3 lifecycle.

CRITICAL: this migration touches ONLY ``cloud_job``.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions; an Alembic
migration touching it would collide -- 020 CRITICAL banner).

Revision ID: 029
Revises: 028
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "029"
down_revision: str | Sequence[str] | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable backend_id then make s3_key nullable (D-06/D-08); no CHECK change, no backfill."""
    op.add_column("cloud_job", sa.Column("backend_id", sa.String(255), nullable=True))
    op.alter_column("cloud_job", "s3_key", existing_type=sa.String(255), nullable=True)  # D-08: compute has no S3 object


def downgrade() -> None:
    """Reverse in reverse order: re-impose s3_key NOT NULL then drop backend_id."""
    op.alter_column("cloud_job", "s3_key", existing_type=sa.String(255), nullable=False)
    op.drop_column("cloud_job", "backend_id")
