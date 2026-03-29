"""Add execution_log table for write-ahead audit trail.

Revision ID: 004
Revises: 003
Create Date: 2026-03-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: str | Sequence[str] | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create execution_log table with FK to proposals, status, and audit fields."""
    op.create_table(
        "execution_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("source_path", sa.Text, nullable=False),
        sa.Column("destination_path", sa.Text, nullable=False),
        sa.Column("sha256_verified", sa.Boolean, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_execution_log"),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"], name="fk_execution_log_proposal_id_proposals"),
    )

    op.create_index("ix_execution_log_proposal_id", "execution_log", ["proposal_id"])
    op.create_index("ix_execution_log_status", "execution_log", ["status"])


def downgrade() -> None:
    """Drop indexes and execution_log table."""
    op.drop_index("ix_execution_log_status", table_name="execution_log")
    op.drop_index("ix_execution_log_proposal_id", table_name="execution_log")
    op.drop_table("execution_log")
