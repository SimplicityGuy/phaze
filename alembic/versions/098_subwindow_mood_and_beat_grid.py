"""Add ``analysis_subwindow`` (sub-window mood) and ``analysis_beat_grid`` (per-file beat grid) (phaze-f6eeb).

Two outputs the analysis already computes and discarded: the per-patch mood matrix, now pooled into
fixed 15 s sub-windows of each coarse window, and the fine pass's beat ticks, stitched into one
file-relative grid. docs/design/0026-subwindow-mood-and-beat-grid.md records the measured patch hops,
the sub-window length, both storage shapes, their measured sizes and the rejected alternatives.

Additive only: no existing table, column or row changes. Both tables start empty -- only a
re-analysis fills them, and the archive-wide re-analysis is deliberately out of this change -- so
there is nothing to backfill and no statistics to seed.

Revision ID: 098
Revises: 097
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "098"
down_revision: str | None = "097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBWINDOW = "analysis_subwindow"
_BEAT_GRID = "analysis_beat_grid"


def upgrade() -> None:
    """Create the two empty child tables of ``files``."""
    op.create_table(
        _SUBWINDOW,
        # ON DELETE CASCADE so these rows never block a file or scan deletion (stage_skip's lesson).
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("sub_index", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("mood_scores", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.PrimaryKeyConstraint("file_id", "window_index", "sub_index"),
        # BARE constraint names: the ck naming convention adds the table prefix (phaze-x8tof).
        sa.CheckConstraint("window_index >= 0 AND sub_index >= 0", name="indexes_nonneg"),
        sa.CheckConstraint("end_sec > start_sec", name="positive_span"),
    )
    op.create_table(
        _BEAT_GRID,
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("beats", postgresql.ARRAY(sa.REAL()), nullable=False),
        sa.Column("beat_count", sa.Integer(), nullable=False),
        sa.Column("gaps", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("beat_count >= 0", name="beat_count_nonneg"),
        sa.CheckConstraint("cardinality(beats) = beat_count", name="beats_match_count"),
    )


def downgrade() -> None:
    """Drop both tables; every existing table is untouched."""
    op.drop_table(_BEAT_GRID)
    op.drop_table(_SUBWINDOW)
