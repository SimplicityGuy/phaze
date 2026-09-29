"""Add ``cloud_job.last_exit_code`` / ``last_failure_reason`` / ``last_failed_at`` (phaze-1xngw).

``cloud_job`` had no column for WHY a cloud pod failed, and the reconcile cron deletes the Job on
every re-drive and at-ceiling spill (``tasks/reconcile_cloud_jobs.py``), so the pod's terminated
``exitCode`` / ``reason`` were gone within about a minute. Spike ``phaze-79mu7`` could tell the
302 files of the 2026-09-26/27 burst failed with exit 10 (``EXIT_DOWNLOAD``) only from the burst
node's containerd journal; the DB alone could not tell a presign failure from an analysis failure.

The reconcile cron now records the most recent no-callback terminal on the row: the pod's
terminated exit code and reason when a pod exists, and an explicit marker (``job_vanished``,
``pending_confirmation_expired``, ``pod_not_found``) in ``last_failure_reason`` with a NULL exit
code when none does.

Pure additive DDL: three NULLABLE columns with no default. Every live row backfills to NULL, which
is exactly right -- no failure was recorded for a row that predates the columns. ``downgrade`` drops
them, losing only diagnostic history, never a budget counter or a status.

Revision ID: 069
Revises: 068
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "069"
down_revision: str | None = "068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cloud_job"

# phaze-gx8p2: nothing filters on these columns. They are written by the reconcile cron and read
# back per row for diagnosis, so a missing ``pg_stats`` row for them cannot mislead any plan.
ANALYZE_EXEMPT_TABLES = {
    _TABLE: "last_exit_code / last_failure_reason / last_failed_at are diagnostic only; no query filters on them.",
}


def upgrade() -> None:
    """Add the three NULLABLE failure-record columns; every existing row backfills to NULL."""
    op.add_column(_TABLE, sa.Column("last_exit_code", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("last_failure_reason", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the three columns. Loses only the recorded failure history, never a budget counter."""
    op.drop_column(_TABLE, "last_failed_at")
    op.drop_column(_TABLE, "last_failure_reason")
    op.drop_column(_TABLE, "last_exit_code")
