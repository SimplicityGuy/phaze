"""Add ``files.original_filename_repaired`` derived mojibake-repair column (phaze-x4ux).

Double-encoded UTF-8 ("mojibake", e.g. 'Sven VÃƒÂ¤th' for 'Sven Väth') in a filename previously
propagated verbatim into search and fuzzy tracklist matching -- ``files.search_vector`` is a
``GENERATED`` column computed directly from ``original_filename``, and application code
(``services/search_queries.py``) builds its file-search ``tsvector`` from that same raw column at
query time. ``original_filename`` itself must stay byte-faithful to what is actually on disk (a
repair fix rewriting it in place would make the DB disagree with the filesystem, and renaming the
real file is the separate, human-approved rename-proposal workflow's job) -- so a repaired form
that search/matching CAN use has to live in its own column.

This migration only adds the column (nullable, DDL-only -- no data migration). Existing rows are
backfilled by the separate, idempotent ``phaze.services.text_repair_backfill`` maintenance
routine (``scripts/backfill_mojibake_filenames.py``): unlike the redrive_attempt-style backfills
elsewhere in this migration chain, populating this column requires the ``repair_mojibake`` Python
codec round trip (no straightforward SQL equivalent), so it is deliberately NOT embedded as
``op.execute`` DML here -- keeping the repair logic in ONE place (the tested Python module)
rather than re-implemented in PL/pgSQL.

New rows are populated at ingest time by ``routers/agent_files.py::upsert_files``.

Revision ID: 045
Revises: 044
Create Date: 2026-07-23
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None

# Static string-literal DDL -- no interpolation, no user input reaches this SQL.
_ADD_COLUMN = "ALTER TABLE public.files ADD COLUMN original_filename_repaired text"
_DROP_COLUMN = "ALTER TABLE public.files DROP COLUMN original_filename_repaired"


def upgrade() -> None:
    """Add the nullable `original_filename_repaired` column (no data migration -- see module docstring)."""
    op.execute(_ADD_COLUMN)


def downgrade() -> None:
    """Drop the `original_filename_repaired` column."""
    op.execute(_DROP_COLUMN)
