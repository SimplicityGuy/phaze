"""Add scan_batches.last_progress_at heartbeat column + backfill existing rows.

The ``last_progress_at`` column is the per-progress heartbeat that drives two
PR4 mechanisms: the admin UI's live activity indicator (green pulsing dot +
"·Ns ago", amber "stalled?" when quiet) and the control-side stall reaper
(``reap_stalled_scans``), which marks a RUNNING batch FAILED once this heartbeat
is older than ``scan_stall_seconds``.

This migration mirrors migration 015's add-column pattern AND migration 016's
data-backfill pattern in a single revision:

  - upgrade(): add the nullable tz-aware column, then backfill every existing
    row's ``last_progress_at`` to its ``updated_at`` so pre-existing rows start
    with a sane non-NULL heartbeat (a RUNNING row created before this migration
    is otherwise immediately "stalled" from the reaper's perspective).
  - downgrade(): drop the column.

Column is TIMESTAMP WITH TIME ZONE to match the runtime type of the
TimestampMixin columns. No NOT NULL constraint -- nullable mirrors completed_at.

Revision ID: 017
Revises: 016
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "017"
down_revision: str | Sequence[str] | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable tz-aware last_progress_at column and backfill from updated_at.

    Raw SQL backfill uses only static literals (no model imports, no untrusted
    input), so there is no injection surface. Only rows whose last_progress_at
    is currently NULL are touched -- the statement is idempotent.
    """
    op.add_column("scan_batches", sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE scan_batches SET last_progress_at = updated_at WHERE last_progress_at IS NULL")


def downgrade() -> None:
    """Drop the last_progress_at column."""
    op.drop_column("scan_batches", "last_progress_at")
