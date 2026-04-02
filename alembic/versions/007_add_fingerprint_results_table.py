"""Add fingerprint_results table.

Revision ID: 007
Revises: 006
Create Date: 2026-04-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: str | Sequence[str] | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create fingerprint_results table."""
    op.create_table(
        "fingerprint_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engine", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fingerprint_results")),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_fingerprint_results_file_id_files")),
    )
    op.create_index("ix_fprint_file_engine", "fingerprint_results", ["file_id", "engine"], unique=True)


def downgrade() -> None:
    """Drop fingerprint_results table."""
    op.drop_index("ix_fprint_file_engine", table_name="fingerprint_results")
    op.drop_table("fingerprint_results")
