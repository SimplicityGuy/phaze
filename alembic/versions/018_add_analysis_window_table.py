"""Add analysis_window table (per-window time-series analysis rows).

Additive-only migration for phase 31. Creates the new ``analysis_window`` child
table of ``files`` (1:many) plus its query indexes. The FK carries
``ON DELETE CASCADE`` so deleting a file removes its windows without orphans.

This migration touches ONLY the new table -- it issues no in-place schema change
against the existing ``analysis`` table, which stays structurally unchanged
(CONTEXT.md). Adding an ORM ``ondelete`` to ``AnalysisResult.file_id`` without a
matching DB-level constraint change would claim a CASCADE Postgres never
enforces, so it is deliberately omitted here and in the model.

Indexes created (5 total):
  - ix_analysis_window_file_tier_idx : composite (file_id, tier, window_index)
  - ix_analysis_window_bpm_fine       : partial on bpm WHERE tier = 'fine'
  - ix_analysis_window_dance_coarse   : partial on danceability WHERE tier = 'coarse'
  - ix_analysis_window_mood           : label index on mood
  - ix_analysis_window_style          : label index on style

No data migration -- the table is new and empty (0 rows).

Revision ID: 018
Revises: 017
Create Date: 2026-06-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "018"
down_revision: str | Sequence[str] | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create analysis_window table and its composite/partial/label indexes."""
    op.create_table(
        "analysis_window",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("bpm", sa.Float(), nullable=True),
        sa.Column("musical_key", sa.String(10), nullable=True),
        sa.Column("mood", sa.String(50), nullable=True),
        sa.Column("style", sa.String(50), nullable=True),
        sa.Column("danceability", sa.Float(), nullable=True),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_window")),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_analysis_window_file_id_files"),
            ondelete="CASCADE",
        ),
    )
    # Composite index: cross-archive scans and per-file ordered window reads.
    op.create_index("ix_analysis_window_file_tier_idx", "analysis_window", ["file_id", "tier", "window_index"])
    # Partial indexes: "files that ever exceed N BPM" (fine) and danceability filters (coarse).
    op.create_index("ix_analysis_window_bpm_fine", "analysis_window", ["bpm"], postgresql_where=sa.text("tier = 'fine'"))
    op.create_index("ix_analysis_window_dance_coarse", "analysis_window", ["danceability"], postgresql_where=sa.text("tier = 'coarse'"))
    # Label indexes: filter/sort by mood and style.
    op.create_index("ix_analysis_window_mood", "analysis_window", ["mood"])
    op.create_index("ix_analysis_window_style", "analysis_window", ["style"])


def downgrade() -> None:
    """Drop the indexes then the analysis_window table (mirror of upgrade)."""
    op.drop_index("ix_analysis_window_style", table_name="analysis_window")
    op.drop_index("ix_analysis_window_mood", table_name="analysis_window")
    op.drop_index("ix_analysis_window_dance_coarse", table_name="analysis_window")
    op.drop_index("ix_analysis_window_bpm_fine", table_name="analysis_window")
    op.drop_index("ix_analysis_window_file_tier_idx", table_name="analysis_window")
    op.drop_table("analysis_window")
