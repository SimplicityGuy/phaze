"""Add file_companions join table.

Revision ID: 003
Revises: 002
Create Date: 2026-03-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: str | Sequence[str] | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create file_companions table with FKs, unique constraint, and indexes."""
    op.create_table(
        "file_companions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("companion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_file_companions"),
        sa.ForeignKeyConstraint(["companion_id"], ["files.id"], name="fk_file_companions_companion_id_files", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_id"], ["files.id"], name="fk_file_companions_media_id_files", ondelete="CASCADE"),
        sa.UniqueConstraint("companion_id", "media_id", name="uq_file_companions_pair"),
    )

    op.create_index("ix_file_companions_companion_id", "file_companions", ["companion_id"])
    op.create_index("ix_file_companions_media_id", "file_companions", ["media_id"])


def downgrade() -> None:
    """Drop indexes and file_companions table."""
    op.drop_index("ix_file_companions_media_id", table_name="file_companions")
    op.drop_index("ix_file_companions_companion_id", table_name="file_companions")
    op.drop_table("file_companions")
