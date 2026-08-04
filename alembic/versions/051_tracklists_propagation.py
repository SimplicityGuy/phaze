"""Let one scraped tracklist be PROPAGATED to a unique set's duplicate files (phaze-fq9h.7).

The drain's whole tractability argument is "look a unique set up ONCE and propagate the answer to
its duplicates": the 1001Tracklists host budget is ~1 request / 8 s for the entire system, and a
re-query per duplicate file spends a request the archive can never get back.

Propagation has to produce a REAL ``tracklists`` row per duplicate file, because ``file_id`` is
what every consumer means by "this file has a tracklist" -- ``services/stage_status.done_clause``,
``services/pipeline.get_untracked_files``, ``routers/tags``, ``routers/cue``. Recording the
propagation in a side table would leave those files reading as un-tracklisted, and the next
operator "search all" would spend a live request on each of them, which is precisely the cost
propagation exists to avoid.

That collides with the old global ``UNIQUE (external_id)``: the propagated rows are the SAME 1001TL
page as the canonical scrape and must carry the same id. The invariant that constraint actually
encoded, though, was narrower -- *one row per page that we scraped* -- so it is re-expressed as a
PARTIAL unique index over the canonical rows, and propagated rows are exempted by carrying a
non-NULL ``propagated_from_set_key``.

Two columns, both additive and nullable, so nothing needs backfilling: every existing row is
canonical by construction, which is exactly what ``propagated_from_set_key IS NULL`` says about it.

* ``propagated_from_set_key`` -- the ``services.tracklist_candidates.set_key`` of the unique set
  that produced the row. It is the discriminator AND the correction handle: dedup is heuristic now
  that audio fingerprinting is gone (epic phaze-0jpe), so a false merge is possible, and one
  ``DELETE ... WHERE propagated_from_set_key = :key`` reverses a bad cluster while leaving the
  canonical scrape intact. It is deliberately NOT a self-referential FK: ``services/scan_deletion``
  cascades tracklists through an explicit ordered statement list, and a DB-level cascade firing
  inside that ordering would delete rows whose versions/tracks the ordering had not yet removed.
* ``propagation_confidence`` -- the ``DuplicateConfidence`` tier of the link that justified the
  write. The drain gates at ``EXACT`` (byte-identical sha256), but "what tier was this written at"
  must be answerable from the ROW rather than from whatever the gate happened to be that month.

Reversible. ``downgrade`` deletes the propagated rows before restoring the global UNIQUE, because
those rows are what makes a global UNIQUE unsatisfiable -- and deleting them is correct rather than
lossy: each is a projection of a canonical row that survives, so the only thing lost is the
duplicate files' link, which a re-drain rebuilds from the cache with ZERO host requests (their
``set_key`` is already ``FOUND`` in ``tracklist_lookup_cache``).

Revision ID: 051
Revises: 050
Create Date: 2026-08-03
"""

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None

_TABLE = "tracklists"
_SET_KEY_COLUMN = "propagated_from_set_key"
_CONFIDENCE_COLUMN = "propagation_confidence"
_EXTERNAL_ID_INDEX = "ix_tracklists_external_id"
_EXTERNAL_ID_CONSTRAINT = "uq_tracklists_external_id"
_SET_KEY_INDEX = "ix_tracklists_propagated_from_set_key"
# Index predicates built from SQLAlchemy constructs rather than `sa.text("... IS NULL")`.
# Both render the identical DDL (`propagated_from_set_key IS NULL`), so the autogenerate drift
# check against the model's matching Index still passes -- but semgrep's
# python.sqlalchemy.security.audit.avoid-sqlalchemy-text flags the text() form, and the honest
# response to that rule is to stop handing SQL to the database as a string rather than to suppress
# it. There is nothing user-supplied here either way; this is simply the form with no string SQL
# in it at all. The DELETE statements in downgrade() keep their literal form for a different
# reason -- see the comment there.
_CANONICAL = sa.column(_SET_KEY_COLUMN).is_(None)
_PROPAGATED = sa.column(_SET_KEY_COLUMN).is_not(None)


def upgrade() -> None:
    """Add the propagation columns and narrow the external_id uniqueness to canonical rows."""
    op.add_column(_TABLE, sa.Column(_SET_KEY_COLUMN, sa.String(length=64), nullable=True))
    op.add_column(_TABLE, sa.Column(_CONFIDENCE_COLUMN, sa.String(length=10), nullable=True))

    # The 039 baseline created BOTH a UNIQUE CONSTRAINT and a redundant UNIQUE INDEX on
    # external_id. Both enforce the global uniqueness this migration is narrowing, so both go --
    # dropping only one would leave the other silently rejecting every propagation.
    op.drop_constraint(_EXTERNAL_ID_CONSTRAINT, _TABLE, type_="unique")
    op.drop_index(_EXTERNAL_ID_INDEX, table_name=_TABLE)
    op.create_index(_EXTERNAL_ID_INDEX, _TABLE, ["external_id"], unique=True, postgresql_where=_CANONICAL)
    op.create_index(_SET_KEY_INDEX, _TABLE, [_SET_KEY_COLUMN], postgresql_where=_PROPAGATED)


def downgrade() -> None:
    """Restore the global UNIQUE, first deleting the rows that make it unsatisfiable.

    The delete is scoped to ``propagated_from_set_key IS NOT NULL``, i.e. exactly the rows this
    migration made possible, and it runs BEFORE the column is dropped so the predicate still exists.
    Their versions/tracks go first, in the child -> parent order ``services/scan_deletion`` uses,
    since ``tracklist_versions.tracklist_id`` and ``tracklist_tracks.version_id`` are real FKs.
    """
    # Written out as literals rather than interpolated from _PROPAGATED: ruff S608 / semgrep's
    # formatted-sql-query rule block ANY f-string-assembled SQL in this repo, and a migration is
    # exactly the wrong place to argue that this particular interpolation happens to be a constant.
    op.execute(
        sa.text(
            "DELETE FROM tracklist_tracks WHERE version_id IN ("
            "  SELECT v.id FROM tracklist_versions v"
            "  JOIN tracklists t ON t.id = v.tracklist_id"
            "  WHERE t.propagated_from_set_key IS NOT NULL)"
        )
    )
    op.execute(sa.text("DELETE FROM tracklist_versions WHERE tracklist_id IN (SELECT id FROM tracklists WHERE propagated_from_set_key IS NOT NULL)"))
    op.execute(sa.text("DELETE FROM tracklists WHERE propagated_from_set_key IS NOT NULL"))

    op.drop_index(_SET_KEY_INDEX, table_name=_TABLE)
    op.drop_index(_EXTERNAL_ID_INDEX, table_name=_TABLE)
    op.create_index(_EXTERNAL_ID_INDEX, _TABLE, ["external_id"], unique=True)
    op.create_unique_constraint(_EXTERNAL_ID_CONSTRAINT, _TABLE, ["external_id"])

    op.drop_column(_TABLE, _CONFIDENCE_COLUMN)
    op.drop_column(_TABLE, _SET_KEY_COLUMN)
