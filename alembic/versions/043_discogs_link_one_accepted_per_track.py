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

_INDEX_NAME = "ix_discogs_links_one_accepted_per_track"
_CREATE = f"CREATE UNIQUE INDEX {_INDEX_NAME} ON public.discogs_links USING btree (track_id) WHERE (status = 'accepted')"
_DROP = f"DROP INDEX {_INDEX_NAME}"


def upgrade() -> None:
    """Add the one-accepted-link-per-track guard."""
    op.execute(_CREATE)


def downgrade() -> None:
    """Drop the one-accepted-link-per-track guard."""
    op.execute(_DROP)
