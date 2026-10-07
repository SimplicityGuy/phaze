"""Persist the chosen detail-page URL as a retry hint on a transient render failure.

Revision ID: 078
Revises: 077
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "078"
down_revision: str | None = "077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nullable, no backfill: existing rows have no hint, so the first retry searches as before."""
    op.add_column("tracklist_lookup_cache", sa.Column("retry_url", sa.Text(), nullable=True))
    op.execute("ANALYZE tracklist_lookup_cache")


def downgrade() -> None:
    """Drop the hint; a retry then re-runs the search, which is the pre-078 behaviour."""
    op.drop_column("tracklist_lookup_cache", "retry_url")
