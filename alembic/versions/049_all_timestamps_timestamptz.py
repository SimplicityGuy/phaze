"""Convert every remaining naive timestamp column to ``timestamptz`` (phaze-cz3m).

Migration 040 (phaze-36rc) fixed ONE table this way after a naive/aware mismatch 500'd
``GET /record/{file_id}``. It left the rest of the split in place, and the split took the cloud
staging scheduler down for ~10 h on 2026-07-30: ``services.pipeline.get_cloud_staging_candidates``
reads its keyset cursor out of the tz-AWARE ``files.created_at`` and binds it back through a param
typed off the model, which declared naive -- so asyncpg encoded an aware value with its naive
``timestamp`` codec and raised ``DataError: can't subtract offset-naive and offset-aware
datetimes``. ``stage_cloud_window`` rolled every tick back and held all 6,918 AWAITING_CLOUD files.

The 039 baseline created these columns inconsistently: 10 tables got ``timestamp with time zone``
and 11 got ``timestamp without time zone``, all from the SAME ``TimestampMixin``. asyncpg decodes by
column OID, so which flavour a row came back as depended on which table it came from -- and the
codebase grew naive/aware-tolerant helpers (``routers.pipeline._elapsed_seconds``,
``test_history_sort_key_tolerates_mixed_tz_awareness``) to paper over the difference. This migration
removes the difference itself, so there is one answer for the whole schema.

Existing naive values were written by ``now()`` under the server session (UTC in deployment), so
they are reinterpreted as UTC via ``AT TIME ZONE 'UTC'`` -- the standard lossless backfill for this
shift, identical to 040's.

``saq_jobs`` is deliberately untouched: its time columns are SAQ-owned bigint epochs, not timestamps.

Revision ID: 049
Revises: 048
Create Date: 2026-07-30
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None

# Static string-literal DDL -- column names are NOT parameterizable in ALTER COLUMN, so every
# statement below is a fixed literal with NO interpolation (no user input ever reaches this SQL).
# Spelled out one statement per column rather than generated from a table/column mapping so the
# exact set of touched columns is reviewable in the diff, matching migration 040's discipline.
_UPGRADE_SQL = (
    "ALTER TABLE public.analysis_window ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.analysis_window ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.cloud_job ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.cloud_job ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.dedup_resolution ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.dedup_resolution ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.discogs_links ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.discogs_links ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.pipeline_stage_control ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.pipeline_stage_control ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.route_control ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.route_control ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.scheduling_ledger ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.scheduling_ledger ALTER COLUMN enqueued_at TYPE timestamp with time zone USING enqueued_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.scheduling_ledger ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.stage_skip ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.stage_skip ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_tracks ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_tracks ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_versions ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_versions ALTER COLUMN scraped_at TYPE timestamp with time zone USING scraped_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_versions ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklists ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklists ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC'",
)
_DOWNGRADE_SQL = (
    "ALTER TABLE public.analysis_window ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.analysis_window ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.cloud_job ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.cloud_job ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.dedup_resolution ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.dedup_resolution ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.discogs_links ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.discogs_links ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.pipeline_stage_control ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.pipeline_stage_control ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.route_control ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.route_control ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.scheduling_ledger ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.scheduling_ledger ALTER COLUMN enqueued_at TYPE timestamp without time zone USING enqueued_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.scheduling_ledger ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.stage_skip ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.stage_skip ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_tracks ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_tracks ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_versions ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_versions ALTER COLUMN scraped_at TYPE timestamp without time zone USING scraped_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklist_versions ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklists ALTER COLUMN created_at TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC'",
    "ALTER TABLE public.tracklists ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC'",
)


def upgrade() -> None:
    """Reinterpret every remaining naive timestamp column as UTC-aware ``timestamptz``."""
    for statement in _UPGRADE_SQL:
        op.execute(statement)


def downgrade() -> None:
    """Return those columns to naive ``timestamp`` (dropping the UTC offset back to wall-clock UTC)."""
    for statement in _DOWNGRADE_SQL:
        op.execute(statement)
