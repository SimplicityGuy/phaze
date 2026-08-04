"""Create ``tracklist_priority_flags`` -- the persisted operator lookup-priority flag (phaze-fq9h.8).

``services.tracklist_drain.DrainCandidate.priority`` has sorted a ``flagged`` set ahead of
everything else since phaze-fq9h.7, and ``tasks.tracklist_drain.drain_tracklists`` has taken a
``flagged_file_ids`` argument since the same bead -- but neither had anywhere to READ a flag from
across more than the one job it was passed into. This table is that home: the admin UI
(phaze-fq9h.8) writes one row per file the operator wants answered first, and
``services.tracklist_drain.build_drain_queue`` reads it on every call so the priority survives
restarts, cron runs, and the operator closing their browser.

Pure additive DDL: one new table, no changes to any existing one, nothing to backfill (an empty
table is exactly correct -- it means "nobody has flagged anything yet"). ``file_id`` is BOTH the
primary key and the only column beyond the inherited timestamps: flagging is a per-file boolean,
not an event log, so a re-flag is an upsert on this same row rather than a new one.
``ON DELETE CASCADE`` keeps a deleted file from leaving an orphaned flag behind.

Revision ID: 052
Revises: 051
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None

_TABLE = "tracklist_priority_flags"


def upgrade() -> None:
    """Create the priority-flag table."""
    op.create_table(
        _TABLE,
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE", name="fk_tracklist_priority_flags_file_id_files"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    """Drop the priority-flag table. Loses nothing but the operator's queue-ordering preference --
    every flagged file's OWN answer, if any, lives in ``tracklists`` / ``tracklist_lookup_cache``
    and is untouched by this."""
    op.drop_table(_TABLE)
