"""Add scan_batches.completed_at terminal-timestamp column.

Stamped once when a ScanBatch reaches a terminal (completed/failed) state so
the admin UI's elapsed timer freezes instead of running forever (incident
260608). Nullable; no backfill needed -- pre-existing terminal rows simply
keep a NULL completed_at and fall back to now-created_at in elapsed_seconds.
Column is TIMESTAMP WITH TIME ZONE to match the runtime type of the
TimestampMixin columns.

Revision ID: 015
Revises: 014
Create Date: 2026-06-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "015"
down_revision: str | Sequence[str] | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable tz-aware completed_at column to scan_batches."""
    op.add_column("scan_batches", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the completed_at column."""
    op.drop_column("scan_batches", "completed_at")
