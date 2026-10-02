"""Track in-flight coarse model-sweep progress separately from completed windows.

Revision ID: 075
Revises: 074
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "075"
down_revision: str | None = "074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
ANALYZE_EXEMPT_TABLES = {
    "analysis": "coarse_work_percent is display-only; no filter, join, or sort reads it, so planner statistics are unnecessary",
}


def upgrade() -> None:
    """Add nullable progress for running analyses."""
    op.add_column("analysis", sa.Column("coarse_work_percent", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove coarse work progress."""
    op.drop_column("analysis", "coarse_work_percent")
