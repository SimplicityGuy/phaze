"""Initial schema - all 5 tables.

Revision ID: 001
Revises:
Create Date: 2026-03-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all 5 tables: files, metadata, analysis, proposals, execution_log."""
    # 1. files - central file record
    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sha256_hash", sa.String(64), nullable=False),
        sa.Column("original_path", sa.Text, nullable=False),
        sa.Column("original_filename", sa.Text, nullable=False),
        sa.Column("current_path", sa.Text, nullable=False),
        sa.Column("file_type", sa.String(10), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("state", sa.String(30), nullable=False, server_default="discovered"),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_files"),
    )
    op.create_index("ix_files_state", "files", ["state"])
    op.create_index("ix_files_sha256_hash", "files", ["sha256_hash"])

    # 2. metadata - extracted tag metadata (1:1 with files)
    op.create_table(
        "metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", name="fk_metadata_file_id_files"), unique=True, nullable=False),
        sa.Column("artist", sa.Text, nullable=True),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("album", sa.Text, nullable=True),
        sa.Column("year", sa.Integer, nullable=True),
        sa.Column("genre", sa.Text, nullable=True),
        sa.Column("raw_tags", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_metadata"),
    )

    # 3. analysis - audio analysis results (1:1 with files)
    op.create_table(
        "analysis",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", name="fk_analysis_file_id_files"), unique=True, nullable=False),
        sa.Column("bpm", sa.Float, nullable=True),
        sa.Column("musical_key", sa.String(10), nullable=True),
        sa.Column("mood", sa.String(50), nullable=True),
        sa.Column("style", sa.String(50), nullable=True),
        sa.Column("fingerprint", sa.Text, nullable=True),
        sa.Column("features", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_analysis"),
    )

    # 4. proposals - AI-generated rename/move proposals
    op.create_table(
        "proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", name="fk_proposals_file_id_files"), nullable=False),
        sa.Column("proposed_filename", sa.Text, nullable=False),
        sa.Column("proposed_path", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("context_used", postgresql.JSONB, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_proposals"),
    )
    op.create_index("ix_proposals_status", "proposals", ["status"])

    # 5. execution_log - append-only audit trail
    op.create_table(
        "execution_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposals.id", name="fk_execution_log_proposal_id_proposals"),
            nullable=False,
        ),
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
    )


def downgrade() -> None:
    """Drop all 5 tables in reverse dependency order."""
    op.drop_table("execution_log")
    op.drop_table("proposals")
    op.drop_table("analysis")
    op.drop_table("metadata")
    op.drop_table("files")
