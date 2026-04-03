"""Add tag_write_log table for tag write audit trail.

Revision ID: 011
Revises: 010
Create Date: 2026-04-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "011"
down_revision: str | Sequence[str] | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create tag_write_log table with indexes."""
    op.create_table(
        "tag_write_log",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id"), nullable=False),
        sa.Column("before_tags", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("after_tags", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("discrepancies", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("written_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_tag_write_log"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_tag_write_log_file_id_files"),
    )

    op.create_index("ix_tag_write_log_file_id", "tag_write_log", ["file_id"])
    op.create_index("ix_tag_write_log_status", "tag_write_log", ["status"])


def downgrade() -> None:
    """Drop tag_write_log table."""
    op.drop_index("ix_tag_write_log_status", table_name="tag_write_log")
    op.drop_index("ix_tag_write_log_file_id", table_name="tag_write_log")
    op.drop_table("tag_write_log")
