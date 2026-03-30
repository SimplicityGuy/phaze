"""Add indexes to execution_log table.

Revision ID: 004
Revises: 003
Create Date: 2026-03-29
"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | Sequence[str] | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add indexes to execution_log table (table already created in migration 001)."""
    op.create_index("ix_execution_log_proposal_id", "execution_log", ["proposal_id"])
    op.create_index("ix_execution_log_status", "execution_log", ["status"])


def downgrade() -> None:
    """Drop indexes from execution_log table."""
    op.drop_index("ix_execution_log_status", table_name="execution_log")
    op.drop_index("ix_execution_log_proposal_id", table_name="execution_log")
