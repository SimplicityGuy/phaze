"""Persist terminal outcomes for analysis attempts without replacing older good results.

Revision ID: 077
Revises: 076
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "077"
down_revision: str | None = "076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Existing obligations have unknown outcomes; new agents acknowledge attempt identity."""
    op.add_column("scheduling_ledger", sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("ANALYZE scheduling_ledger")


def downgrade() -> None:
    """Remove the per-attempt terminal outcome."""
    op.drop_column("scheduling_ledger", "terminal_at")
