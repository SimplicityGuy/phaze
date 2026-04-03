"""Add search_vector GENERATED columns and GIN indexes for full-text search.

Revision ID: 009
Revises: 008
Create Date: 2026-04-02
"""

from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: str | Sequence[str] | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add tsvector columns, GIN indexes, and pg_trgm trigram indexes."""
    # Enable pg_trgm extension for trigram similarity / ILIKE optimization
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Add GENERATED STORED tsvector column to files table
    op.execute(
        """
        ALTER TABLE files ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(original_filename, ''))
        ) STORED
        """
    )

    # Add GENERATED STORED tsvector column to metadata table
    op.execute(
        """
        ALTER TABLE metadata ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple',
                coalesce(artist, '') || ' ' ||
                coalesce(title, '') || ' ' ||
                coalesce(album, '') || ' ' ||
                coalesce(genre, '')
            )
        ) STORED
        """
    )

    # Add GENERATED STORED tsvector column to tracklists table
    op.execute(
        """
        ALTER TABLE tracklists ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple',
                coalesce(artist, '') || ' ' ||
                coalesce(event, '')
            )
        ) STORED
        """
    )

    # Create GIN indexes on search_vector columns for full-text search
    op.execute("CREATE INDEX ix_files_search_vector ON files USING gin(search_vector)")
    op.execute("CREATE INDEX ix_metadata_search_vector ON metadata USING gin(search_vector)")
    op.execute("CREATE INDEX ix_tracklists_search_vector ON tracklists USING gin(search_vector)")

    # Create GIN trigram indexes for ILIKE partial matching fallback
    op.execute("CREATE INDEX ix_files_filename_trgm ON files USING gin(original_filename gin_trgm_ops)")
    op.execute("CREATE INDEX ix_metadata_artist_trgm ON metadata USING gin(artist gin_trgm_ops)")
    op.execute("CREATE INDEX ix_tracklists_artist_trgm ON tracklists USING gin(artist gin_trgm_ops)")


def downgrade() -> None:
    """Drop all search indexes, search_vector columns, and pg_trgm extension."""
    # Drop trigram indexes
    op.execute("DROP INDEX IF EXISTS ix_tracklists_artist_trgm")
    op.execute("DROP INDEX IF EXISTS ix_metadata_artist_trgm")
    op.execute("DROP INDEX IF EXISTS ix_files_filename_trgm")

    # Drop GIN indexes on search_vector columns
    op.execute("DROP INDEX IF EXISTS ix_tracklists_search_vector")
    op.execute("DROP INDEX IF EXISTS ix_metadata_search_vector")
    op.execute("DROP INDEX IF EXISTS ix_files_search_vector")

    # Drop search_vector columns
    op.execute("ALTER TABLE tracklists DROP COLUMN IF EXISTS search_vector")
    op.execute("ALTER TABLE metadata DROP COLUMN IF EXISTS search_vector")
    op.execute("ALTER TABLE files DROP COLUMN IF EXISTS search_vector")

    # Drop pg_trgm extension
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
