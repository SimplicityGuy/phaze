"""Add windowed-analysis coverage columns to the analysis table.

Additive-only migration for phase 43. Plan 02's ``analyze_file`` now returns a
five-field coverage contract describing how much of a file the bounded windowed
analysis actually covered:

  - fine_windows_analyzed  : fine-tier windows actually analyzed (Integer)
  - fine_windows_total     : fine-tier windows the file would have at full cover
  - coarse_windows_analyzed: coarse-tier windows actually analyzed (Integer)
  - coarse_windows_total   : coarse-tier windows the file would have at full cover
  - sampled                : True when even-stride sampling capped the window set

This migration adds those five columns to the existing ``analysis`` aggregate
table so the coverage lands in dedicated columns instead of the ``features``
JSONB overflow (Pitfall 3). All five are ``nullable=True`` -- pre-43 rows and
empty-body PUTs leave them NULL, so there is NO data migration (every existing
row simply gets NULL coverage).

``FileState.ANALYSIS_FAILED`` is added in the same plan but needs NO migration:
``FileRecord.state`` is ``String(30)`` storing a ``StrEnum`` value, so a new
enum member is code-only.

Revision ID: 021
Revises: 020
Create Date: 2026-06-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "021"
down_revision: str | Sequence[str] | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the five all-nullable coverage columns to ``analysis``."""
    op.add_column("analysis", sa.Column("fine_windows_analyzed", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("fine_windows_total", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("coarse_windows_analyzed", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("coarse_windows_total", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("sampled", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drop the five coverage columns in reverse order (mirror of upgrade)."""
    op.drop_column("analysis", "sampled")
    op.drop_column("analysis", "coarse_windows_total")
    op.drop_column("analysis", "coarse_windows_analyzed")
    op.drop_column("analysis", "fine_windows_total")
    op.drop_column("analysis", "fine_windows_analyzed")
