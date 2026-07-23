"""Add UNIQUE(tracklist_id, version_number) to tracklist_versions (phaze-5vmt).

A concurrent-scrape race in ``_store_scraped_tracklist`` could compute the same next
``version_number`` for one tracklist from two jobs and INSERT duplicate version rows,
silently orphaning one version's tracks from every reader (which keys off
``latest_version_id``). The primary fix serializes the upsert with a per-``external_id``
advisory lock; this UNIQUE constraint is defense-in-depth so the race fails loudly
(``IntegrityError`` -> SAQ retry) if it ever recurs.

The upgrade FIRST renumbers the losers of any pre-existing (tracklist_id, version_number)
duplicate group: the bug this constraint guards against has been LIVE, so an existing
database can already hold duplicate rows, and creating the unique constraint over them
would abort the migration (phaze-am5p). Per duplicate group, the row referenced by
``tracklists.latest_version_id`` is kept as the winner (falling back to the lowest ``id``
when no row in the group matches), and every other row is renumbered to a fresh,
non-colliding ``version_number`` (current per-tracklist max + an offset) rather than
deleted -- ``fk_tracklist_tracks_version_id_tracklist_versions`` has no ``ON DELETE
CASCADE``, so deleting a version row would orphan its ``tracklist_tracks`` with no clean
way to reparent them. Renumbering keeps every row (and its tracks) intact and total.

Revision ID: 041
Revises: 040
Create Date: 2026-07-17
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "uq_tracklist_versions_tracklist_id_version_number"

# Pre-constraint dedupe (the migration-041 lesson later reapplied at 043/044): renumber, never
# delete, so no row (and no dependent tracklist_tracks row) is lost. Static string-literal DML --
# no interpolation, no user input reaches this SQL.
#
# ``ranked`` orders each (tracklist_id, version_number) duplicate group so the row matching
# ``tracklists.latest_version_id`` sorts first (the intended winner); when no row in the group
# matches (or ``latest_version_id`` is NULL), ties break on ``id`` ascending, so the winner falls
# back to the lowest id. ``losers`` numbers every non-winner row per tracklist (across ALL of its
# duplicate groups) so the reassigned version_numbers are mutually distinct. ``tracklist_max``
# supplies the per-tracklist ceiling; losers are renumbered to max + their rank, which is always
# higher than every existing version_number for that tracklist, so the UPDATE cannot create a new
# collision (with either the surviving rows or with each other).
_DEDUPE_EXISTING = """
WITH ranked AS (
    SELECT
        tv.id,
        tv.tracklist_id,
        ROW_NUMBER() OVER (
            PARTITION BY tv.tracklist_id, tv.version_number
            ORDER BY (tv.id = t.latest_version_id) DESC, tv.id
        ) AS dup_rank
    FROM public.tracklist_versions tv
    JOIN public.tracklists t ON t.id = tv.tracklist_id
),
losers AS (
    SELECT
        id,
        tracklist_id,
        ROW_NUMBER() OVER (PARTITION BY tracklist_id ORDER BY id) AS loser_rank
    FROM ranked
    WHERE dup_rank > 1
),
tracklist_max AS (
    SELECT tracklist_id, MAX(version_number) AS max_version_number
    FROM public.tracklist_versions
    GROUP BY tracklist_id
)
UPDATE public.tracklist_versions tv
SET version_number = tracklist_max.max_version_number + losers.loser_rank
FROM losers
JOIN tracklist_max ON tracklist_max.tracklist_id = losers.tracklist_id
WHERE tv.id = losers.id
"""


def upgrade() -> None:
    """Renumber pre-existing duplicate versions, then add the (tracklist_id, version_number) uniqueness guard."""
    op.execute(_DEDUPE_EXISTING)
    op.create_unique_constraint(_CONSTRAINT_NAME, "tracklist_versions", ["tracklist_id", "version_number"])


def downgrade() -> None:
    """Drop the (tracklist_id, version_number) uniqueness guard (renumbered duplicates stay renumbered)."""
    op.drop_constraint(_CONSTRAINT_NAME, "tracklist_versions", type_="unique")
