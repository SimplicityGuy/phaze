"""Migrate ``tag_write_log`` timestamp columns to ``timestamp with time zone`` (phaze-36rc).

``tag_write_log`` was the only table whose ``written_at`` / ``created_at`` / ``updated_at`` columns
were declared ``timestamp without time zone`` (039 baseline:268-270), while sibling audit tables such
as ``execution_log`` (039:142-144) use ``timestamp with time zone``. asyncpg decodes by column OID, so
``execution_log.executed_at`` came back tz-AWARE and ``tag_write_log.written_at`` tz-NAIVE. Merging both
histories in ``GET /record/{file_id}`` then raised ``TypeError: can't compare offset-naive and
offset-aware datetimes`` -> unhandled 500 on the HAPPY PATH (every tag-written file also carries an
execution log). This migration aligns ``tag_write_log`` with the rest of the schema.

Existing naive values were written by ``now()`` under the server session (UTC in deployment), so they
are reinterpreted as UTC via ``AT TIME ZONE 'UTC'`` -- the standard lossless backfill for this shift.

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

_COLUMNS = ("written_at", "created_at", "updated_at")


def upgrade() -> None:
    """Reinterpret the naive ``tag_write_log`` timestamps as UTC-aware ``timestamptz``."""
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE public.tag_write_log ALTER COLUMN {column} TYPE timestamp with time zone USING {column} AT TIME ZONE 'UTC'")


def downgrade() -> None:
    """Return the columns to naive ``timestamp`` (dropping the UTC offset back to wall-clock UTC)."""
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE public.tag_write_log ALTER COLUMN {column} TYPE timestamp without time zone USING {column} AT TIME ZONE 'UTC'")
