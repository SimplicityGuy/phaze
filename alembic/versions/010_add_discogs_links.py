"""Add discogs_links table for Discogs release candidate matching.

Revision ID: 010
Revises: 009
Create Date: 2026-04-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "010"
down_revision: str | Sequence[str] | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create discogs_links table with indexes including GIN FTS index."""
    op.create_table(
        "discogs_links",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("track_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("tracklist_tracks.id"), nullable=False),
        sa.Column("discogs_release_id", sa.String(50), nullable=False),
        sa.Column("discogs_artist", sa.Text, nullable=True),
        sa.Column("discogs_title", sa.Text, nullable=True),
        sa.Column("discogs_label", sa.Text, nullable=True),
        sa.Column("discogs_year", sa.Integer, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="candidate"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_discogs_links"),
        sa.ForeignKeyConstraint(["track_id"], ["tracklist_tracks.id"], name="fk_discogs_links_track_id_tracklist_tracks"),
    )

    op.create_index("ix_discogs_links_track_id", "discogs_links", ["track_id"])
    op.create_index("ix_discogs_links_status", "discogs_links", ["status"])
    op.create_index("ix_discogs_links_discogs_release_id", "discogs_links", ["discogs_release_id"])

    # GIN full-text search index on denormalized artist + title (D-09)
    op.execute(
        "CREATE INDEX ix_discogs_links_fts ON discogs_links "
        "USING GIN (to_tsvector('simple', coalesce(discogs_artist, '') || ' ' || coalesce(discogs_title, '')))"
    )


def downgrade() -> None:
    """Drop discogs_links table."""
    op.execute("DROP INDEX IF EXISTS ix_discogs_links_fts")
    op.drop_index("ix_discogs_links_discogs_release_id", table_name="discogs_links")
    op.drop_index("ix_discogs_links_status", table_name="discogs_links")
    op.drop_index("ix_discogs_links_track_id", table_name="discogs_links")
    op.drop_table("discogs_links")
