"""Add source, status columns to tracklists and confidence to tracklist_tracks.

Revision ID: 008
Revises: 007
Create Date: 2026-04-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: str | Sequence[str] | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add source and status to tracklists, confidence to tracklist_tracks."""
    op.add_column("tracklists", sa.Column("source", sa.String(30), nullable=False, server_default="1001tracklists"))
    op.add_column("tracklists", sa.Column("status", sa.String(20), nullable=False, server_default="approved"))
    op.add_column("tracklist_tracks", sa.Column("confidence", sa.Float(), nullable=True))
    op.create_index("ix_tracklists_source", "tracklists", ["source"])
    op.create_index("ix_tracklists_status", "tracklists", ["status"])


def downgrade() -> None:
    """Remove source, status, confidence columns and indexes."""
    op.drop_index("ix_tracklists_status", table_name="tracklists")
    op.drop_index("ix_tracklists_source", table_name="tracklists")
    op.drop_column("tracklist_tracks", "confidence")
    op.drop_column("tracklists", "status")
    op.drop_column("tracklists", "source")
