"""Add partial UNIQUE(track_id) WHERE status='accepted' to discogs_links (phaze-gl1k).

``bulk_link_discogs`` loaded only ``status == 'candidate'`` rows, then per track accepted the
top candidate and dismissed only the OTHER rows in that same candidate-only working set. A
pre-existing 'accepted' link for the track (left behind because ``match_tracklist_to_discogs``
deliberately preserves accepted links and deletes only candidate-status rows) was never in that
working set, so it was neither dismissed nor considered -- the track ended up with TWO rows in
status 'accepted', silently corrupting which label/year a later CUE generation or tag write
picked (D-07: "one accepted link per track enforced at application level").

The primary fix broadens ``bulk_link_discogs`` (and reorders ``accept_discogs_link``) to dismiss
every OTHER non-dismissed link for a track, status-blind, before accepting the winner. This
partial unique index is defense-in-depth so a concurrent double-accept race fails loudly with an
``IntegrityError`` instead of silently landing two accepted rows for the same track.

The upgrade FIRST dismisses all but the newest accepted row per track (newest ``updated_at``,
``id`` tiebreaker -- the same winner ``accept_discogs_link`` would keep): the bug this index
guards against has been LIVE, so an existing database can already hold double-accepted rows,
and creating the unique index over them would abort the migration (the migration-041 lesson,
phaze-am5p). Deduplicating in the same transaction makes the index creation total.

Revision ID: 043
Revises: 042
Create Date: 2026-07-22
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None

# Static string-literal DDL/DML -- no interpolation, no user input reaches this SQL.
_DEDUPE_EXISTING = """
UPDATE public.discogs_links dl
SET status = 'dismissed'
WHERE dl.status = 'accepted'
  AND EXISTS (
    SELECT 1
    FROM public.discogs_links newer
    WHERE newer.track_id = dl.track_id
      AND newer.status = 'accepted'
      AND (newer.updated_at, newer.id) > (dl.updated_at, dl.id)
  )
"""
_CREATE = "CREATE UNIQUE INDEX ix_discogs_links_one_accepted_per_track ON public.discogs_links USING btree (track_id) WHERE (status = 'accepted')"
_DROP = "DROP INDEX ix_discogs_links_one_accepted_per_track"


def upgrade() -> None:
    """Dedupe pre-existing double-accepted rows, then add the one-accepted-link-per-track guard."""
    op.execute(_DEDUPE_EXISTING)
    op.execute(_CREATE)


def downgrade() -> None:
    """Drop the one-accepted-link-per-track guard (dismissed duplicates stay dismissed)."""
    op.execute(_DROP)
