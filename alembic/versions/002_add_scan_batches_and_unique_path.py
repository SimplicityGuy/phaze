"""Add scan_batches table and unique path index.

Revision ID: 002
Revises: 001
Create Date: 2026-03-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | Sequence[str] | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create scan_batches table, add unique index on files.original_path, add FK from files.batch_id."""
    # 1. Create scan_batches table
    op.create_table(
        "scan_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scan_path", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("total_files", sa.Integer, server_default="0", nullable=False),
        sa.Column("processed_files", sa.Integer, server_default="0", nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scan_batches"),
    )

    # 2. Add unique index on files.original_path for resumability (INSERT ON CONFLICT)
    op.create_index("uq_files_original_path", "files", ["original_path"], unique=True)

    # 3. Add foreign key from files.batch_id to scan_batches.id
    op.create_foreign_key("fk_files_batch_id_scan_batches", "files", "scan_batches", ["batch_id"], ["id"])


def downgrade() -> None:
    """Drop FK, unique index, and scan_batches table."""
    op.drop_constraint("fk_files_batch_id_scan_batches", "files", type_="foreignkey")
    op.drop_index("uq_files_original_path", table_name="files")
    op.drop_table("scan_batches")
