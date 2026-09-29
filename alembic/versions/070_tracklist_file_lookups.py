"""Add ``tracklist_file_lookups`` -- the per-file record of what the 1001TL drain did for each file (phaze-o71bf).

The drain's cache (``tracklist_lookup_cache``) is keyed by a runtime hash of a derived query plus a
duration bucket, so nothing in the database linked a FILE to its lookup: the Files page and the
agent stage table could show nothing about tracklists, and ``Stage.TRACKLIST`` could only ever read
``done`` (a ``tracklists`` row) or ``not_started``. This table is that link, one row per file,
written by the drain; ``services/stage_status`` derives TRACKLIST's in-flight / failed / skipped
buckets from it.

Pure additive DDL: a new table with no rows. No backfill -- the first drain slice after deploy
writes a row for every media file its queue build sees, and until then every file reads exactly
what it read before this migration (``done`` with a tracklist, ``not_started`` without).
``downgrade`` drops the table, losing only per-file outcome history the next slice rewrites.

Revision ID: 070
Revises: 069
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "070"
down_revision: str | None = "069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "tracklist_file_lookups"


def upgrade() -> None:
    """Create the per-file lookup table, keyed and cascaded on ``files.id``."""
    op.create_table(
        _TABLE,
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("set_key", sa.String(length=64), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_tracklist_file_lookups_file_id_files", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", name="pk_tracklist_file_lookups"),
    )


def downgrade() -> None:
    """Drop the per-file lookup table."""
    op.drop_table(_TABLE)
