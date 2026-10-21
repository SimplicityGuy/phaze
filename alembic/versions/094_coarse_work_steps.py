"""Carry the coarse work-step counters behind ``coarse_work_percent`` (phaze-s03fz).

``analysis.coarse_work_done`` / ``analysis.coarse_work_total`` are the "coarse_work" heartbeat's done and total, the
exact counters ``coarse_work_percent`` is computed from, so the analyze queue can show ``step a/T`` beside the
percentage without reconstructing either from the lossy percent. Both are NULL for every existing row and for an
agent that predates the fields.

Revision ID: 094
Revises: 093
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "094"
down_revision: str | None = "093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ANALYZE_EXEMPT_TABLES = {
    "analysis": "coarse_work_done/coarse_work_total are display-only; no filter, join, or sort reads them, so planner statistics are unnecessary",
}


def upgrade() -> None:
    """Add nullable coarse work-step counters."""
    op.add_column("analysis", sa.Column("coarse_work_done", sa.Integer(), nullable=True))
    op.add_column("analysis", sa.Column("coarse_work_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove the coarse work-step counters."""
    op.drop_column("analysis", "coarse_work_total")
    op.drop_column("analysis", "coarse_work_done")
