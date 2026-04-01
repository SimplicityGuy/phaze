"""Add tracklists, tracklist_versions, and tracklist_tracks tables.

Revision ID: 006
Revises: 005
Create Date: 2026-04-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: str | Sequence[str] | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create tracklists, tracklist_versions, and tracklist_tracks tables."""
    op.create_table(
        "tracklists",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.String(50), unique=True, nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id"), nullable=True),
        sa.Column("match_confidence", sa.Integer(), nullable=True),
        sa.Column("auto_linked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("event", sa.Text(), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("latest_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracklists")),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_tracklists_file_id_files")),
        sa.UniqueConstraint("external_id", name=op.f("uq_tracklists_external_id")),
    )
    op.create_index(op.f("ix_tracklists_file_id"), "tracklists", ["file_id"])
    op.create_index(op.f("ix_tracklists_external_id"), "tracklists", ["external_id"], unique=True)

    op.create_table(
        "tracklist_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tracklist_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("scraped_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracklist_versions")),
        sa.ForeignKeyConstraint(["tracklist_id"], ["tracklists.id"], name=op.f("fk_tracklist_versions_tracklist_id_tracklists")),
    )

    op.create_table(
        "tracklist_tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.String(20), nullable=True),
        sa.Column("is_mashup", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("remix_info", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracklist_tracks")),
        sa.ForeignKeyConstraint(["version_id"], ["tracklist_versions.id"], name=op.f("fk_tracklist_tracks_version_id_tracklist_versions")),
    )
    op.create_index(op.f("ix_tracklist_tracks_version_id"), "tracklist_tracks", ["version_id"])


def downgrade() -> None:
    """Drop tracklist_tracks, tracklist_versions, and tracklists tables."""
    op.drop_index(op.f("ix_tracklist_tracks_version_id"), table_name="tracklist_tracks")
    op.drop_table("tracklist_tracks")
    op.drop_table("tracklist_versions")
    op.drop_index(op.f("ix_tracklists_external_id"), table_name="tracklists")
    op.drop_index(op.f("ix_tracklists_file_id"), table_name="tracklists")
    op.drop_table("tracklists")
