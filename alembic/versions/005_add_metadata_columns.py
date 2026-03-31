"""Add track_number, duration, bitrate columns to metadata table.

Revision ID: 005
Revises: 004
Create Date: 2026-03-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: str | Sequence[str] | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add track_number, duration, bitrate columns to metadata table."""
    op.add_column("metadata", sa.Column("track_number", sa.Integer(), nullable=True))
    op.add_column("metadata", sa.Column("duration", sa.Float(), nullable=True))
    op.add_column("metadata", sa.Column("bitrate", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove track_number, duration, bitrate columns from metadata table."""
    op.drop_column("metadata", "bitrate")
    op.drop_column("metadata", "duration")
    op.drop_column("metadata", "track_number")
