"""Add ``ix_files_created_at_id`` -- the cloud staging walk's FIFO order as an index (phaze-2al7q.3).

``services.pipeline.cloud.get_cloud_staging_candidates`` walks awaiting-cloud files in ``(created_at, id)``
order with a keyset cursor, ``LIMIT`` rows at a time, under ``FOR UPDATE ... SKIP LOCKED``. Without an index
on that order the plan sorted EVERY awaiting row on every page (``LockRows`` sits between ``Limit`` and
``Sort``, so the sort cannot be a bounded top-N). Measured on a scratch database seeded at production scale
(170,410 files, 70,410 awaiting): 440 ms per head page without the index, 0.25 ms with it (an index scan that
stops after ``LIMIT`` rows). Each tick may fetch several pages while holding the drain's advisory lock.

Index-only DDL; no data migration. Built CONCURRENTLY with an autocommit connection and the INVALID-leftover
self-heal, mirroring migrations 058 and 081: ``files`` takes every scan and watcher insert, and a plain
``CREATE INDEX`` would block them for the length of the build.

Revision ID: 097
Revises: 096
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op


revision: str = "097"
down_revision: str | None = "096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Static string-literal DDL -- no interpolation, no user input reaches this SQL (mirrors migrations 058 and 081).
_CREATE_INDEX = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_files_created_at_id ON public.files USING btree (created_at, id)"
_DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS public.ix_files_created_at_id"
_CHECK_INVALID = "SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass('public.ix_files_created_at_id')"


def upgrade() -> None:
    """Create the FIFO index concurrently, self-healing an INVALID leftover first."""
    with op.get_context().autocommit_block():
        if not context.is_offline_mode() and op.get_bind().execute(sa.text(_CHECK_INVALID)).scalar():
            op.execute(_DROP_INDEX)
        op.execute(_CREATE_INDEX)


def downgrade() -> None:
    """Drop the index concurrently."""
    with op.get_context().autocommit_block():
        op.execute(_DROP_INDEX)
