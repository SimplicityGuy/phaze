"""Add ``cloud_job.staging_bucket`` (Phase 70, D-01/D-02, MKUE-04).

Additive, reversible migration -- the per-file staging-bucket record (MKUE-04). It adds a single
nullable column to ``cloud_job``:

* ``staging_bucket`` (``String(255)``, NULL) -- records which ``BucketConfig.id`` staged the current
  object (D-01/D-06). It is added NULLABLE with **NO backfill** -- the a1/k8s paths were never
  deployed live so there are ~zero rows to migrate, and a migration cannot know a per-file bucket
  choice; new rows stamp it going forward. Plain free-text: **no CHECK/enum change** (mirrors 029's
  ``backend_id``). The recorded value is authoritative -- presign/cleanup READ it, never re-derive.

D-02: ``cloud_job`` stays one-row-per-file -- ``unique(file_id)`` is untouched.

CRITICAL: this migration touches ONLY ``cloud_job``.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions; an Alembic
migration touching it would collide -- 020/029 CRITICAL banner).

Revision ID: 030
Revises: 029
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "030"
down_revision: str | Sequence[str] | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable staging_bucket (D-01/D-06); no CHECK change, no backfill."""
    op.add_column("cloud_job", sa.Column("staging_bucket", sa.String(255), nullable=True))


def downgrade() -> None:
    """Drop staging_bucket."""
    op.drop_column("cloud_job", "staging_bucket")
