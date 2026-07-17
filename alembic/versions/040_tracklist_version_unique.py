"""Add UNIQUE(tracklist_id, version_number) to tracklist_versions (phaze-5vmt).

A concurrent-scrape race in ``_store_scraped_tracklist`` could compute the same next
``version_number`` for one tracklist from two jobs and INSERT duplicate version rows,
silently orphaning one version's tracks from every reader (which keys off
``latest_version_id``). The primary fix serializes the upsert with a per-``external_id``
advisory lock; this UNIQUE constraint is defense-in-depth so the race fails loudly
(``IntegrityError`` -> SAQ retry) if it ever recurs.

Revision ID: 040
Revises: 039
Create Date: 2026-07-17
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "uq_tracklist_versions_tracklist_id_version_number"


def upgrade() -> None:
    """Add the (tracklist_id, version_number) uniqueness guard."""
    op.create_unique_constraint(_CONSTRAINT_NAME, "tracklist_versions", ["tracklist_id", "version_number"])


def downgrade() -> None:
    """Drop the (tracklist_id, version_number) uniqueness guard."""
    op.drop_constraint(_CONSTRAINT_NAME, "tracklist_versions", type_="unique")
