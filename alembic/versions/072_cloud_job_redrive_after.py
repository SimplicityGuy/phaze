"""Add ``cloud_job.redrive_after`` (phaze-d28sn).

The reconcile cron re-drove a failed cloud submit on the next one-minute tick, so a file spent its
whole budget -- one submit and three re-drives -- in about three minutes, and any outage longer than
that exhausted every in-flight file (spike ``phaze-79mu7``). A charged re-drive now waits a
configurable backoff (``cloud_redrive_backoff_sec``) before its fresh submit is enqueued. This column
records when that wait ends, and is the explicit marker that separates a row waiting out its backoff
from one whose resubmit is already queued: both are SUBMITTED with no ``kueue_workload``.

Pure additive DDL: one NULLABLE column with no default. Every live row backfills to NULL, meaning "not
waiting", which is exactly the pre-migration behaviour. ``downgrade`` drops it; a row caught waiting at
that moment is then read as pending confirmation, so once the pending-submit bound expires it takes the
charged no-callback terminal and spends one more attempt.

Revision ID: 072
Revises: 071
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "072"
down_revision: str | None = "071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cloud_job"

# phaze-gx8p2: nothing filters on this column. The reconcile cron reads it back per row it has already
# selected by status and backend, so a missing ``pg_stats`` row for it cannot mislead any plan.
ANALYZE_EXEMPT_TABLES = {
    _TABLE: "redrive_after is read per row by the reconcile cron; no query filters on it.",
}


def upgrade() -> None:
    """Add the NULLABLE backoff deadline; every existing row backfills to NULL (not waiting)."""
    op.add_column(_TABLE, sa.Column("redrive_after", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the column. A row waiting at that moment falls back to the pending-confirmation path (see above)."""
    op.drop_column(_TABLE, "redrive_after")
