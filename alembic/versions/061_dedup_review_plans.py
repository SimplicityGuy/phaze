"""Add durable duplicate review plans.

Revision ID: 061
Revises: 060
Create Date: 2026-08-15
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the immutable review-plan and commit-audit table."""
    op.create_table(
        "dedup_review_plan",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_hash", sa.Text(), nullable=False),
        sa.Column("canonical_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop duplicate review plans."""
    op.drop_table("dedup_review_plan")
