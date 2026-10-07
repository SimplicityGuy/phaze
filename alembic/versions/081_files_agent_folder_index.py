"""Add ``ix_files_agent_id_folder`` -- the files of one agent by the folder they sit in (phaze-4x319.5).

The known-stamp grouping and the junk-review detector decide "does this folder hold media" from the
``files`` rows at decision time (``services/companion_content.py::media_in_folders``), the same rows
the linking chain reads, instead of the folder listing an agent took when it read the companion.
That read selects media by folder, ``regexp_replace(original_path, '/[^/]*$', '') IN (...)``; under
the database's default (non-C) collation the existing ``(agent_id, original_path)`` index cannot
serve a path-prefix match, so without this index every page of folders would scan all of an agent's
rows.

Index-only DDL; no data migration. Built CONCURRENTLY with an autocommit connection and the
INVALID-leftover self-heal, mirroring migration 058: ``files`` takes every scan and watcher insert,
and a plain ``CREATE INDEX`` would block them for the length of the build.

Revision ID: 081
Revises: 080
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op


revision: str = "081"
down_revision: str | None = "080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Static string-literal DDL -- no interpolation, no user input reaches this SQL (mirrors migration 058).
_CREATE_INDEX = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_files_agent_id_folder ON public.files USING btree (agent_id, regexp_replace(original_path, '/[^/]*$', ''))"
_DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS public.ix_files_agent_id_folder"
_CHECK_INVALID = "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass('public.ix_files_agent_id_folder')"


def upgrade() -> None:
    """Create the folder index concurrently, self-healing an INVALID leftover first."""
    with op.get_context().autocommit_block():
        if not context.is_offline_mode() and op.get_bind().execute(sa.text(_CHECK_INVALID)).scalar():
            op.execute(_DROP_INDEX)
        op.execute(_CREATE_INDEX)


def downgrade() -> None:
    """Drop the index concurrently."""
    with op.get_context().autocommit_block():
        op.execute(_DROP_INDEX)
