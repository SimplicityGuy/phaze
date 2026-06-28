"""Add the Kube submit/reconcile lifecycle columns to ``cloud_job`` (Phase 54, D-09).

Additive-only, reversible migration. Extends the Phase 53 ``cloud_job`` staging sidecar with
the columns the Kube submit + reconcile cron need:

* ``kueue_workload`` (``String(255)``, NULL) -- the Kueue/Job name stamped at submit; the
  reconcile cron looks the Job up by this name.
* ``attempts`` (``Integer``, NOT NULL, default ``0``) -- the bounded re-drive counter (D-08);
  once it exceeds ``cloud_submit_max_attempts`` the file is marked ANALYSIS_FAILED.
* ``inadmissible`` (``Boolean``, NOT NULL, default ``false``) -- drives the D-06 operator alert
  when the Kueue Workload is Inadmissible.

It also swaps the ``status`` CHECK to the 6-member list so the new SUBMITTED/RUNNING/SUCCEEDED
``CloudJobStatus`` members are accepted (string-backed enum -- only the CHECK membership list
changes, no Postgres enum-type migration). ``cloud_phase`` is deliberately NOT added here
(reserved for Phase 55's own migration).

The ``status`` CHECK (declared name ``status_enum``, auto-prefixed to ``ck_cloud_job_status_enum``
by the naming convention) is the authoritative membership gate for the string-backed
``CloudJobStatus`` StrEnum.

CRITICAL: this migration touches ONLY ``cloud_job``.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions; an Alembic
migration touching it would collide -- 020 CRITICAL banner).

Revision ID: 026
Revises: 025
Create Date: 2026-06-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "026"
down_revision: str | Sequence[str] | None = "025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_ENUM_NEW = "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed')"
_STATUS_ENUM_OLD = "status IN ('uploading', 'uploaded', 'failed')"


def upgrade() -> None:
    """Add the 3 kube columns then widen the status CHECK to the 6-member list."""
    op.add_column("cloud_job", sa.Column("kueue_workload", sa.String(255), nullable=True))
    op.add_column("cloud_job", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("cloud_job", sa.Column("inadmissible", sa.Boolean(), server_default=sa.false(), nullable=False))
    # Bare name ``status_enum`` -- the ``ck_%(table_name)s_%(constraint_name)s`` naming convention
    # re-applies the ``ck_cloud_job_`` prefix (passing the already-prefixed name double-prefixes it).
    op.drop_constraint("status_enum", "cloud_job", type_="check")
    op.create_check_constraint("status_enum", "cloud_job", _STATUS_ENUM_NEW)


def downgrade() -> None:
    """Restore the original 3-member status CHECK then drop the 3 columns (reverse order)."""
    op.drop_constraint("status_enum", "cloud_job", type_="check")
    op.create_check_constraint("status_enum", "cloud_job", _STATUS_ENUM_OLD)
    op.drop_column("cloud_job", "inadmissible")
    op.drop_column("cloud_job", "attempts")
    op.drop_column("cloud_job", "kueue_workload")
