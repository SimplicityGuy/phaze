"""Add partial UNIQUE(agent_id, scan_path) WHERE status='running' to scan_batches (phaze-1a71).

``trigger_scan`` unconditionally created a fresh RUNNING ``ScanBatch`` and enqueued
``scan_directory`` (a deterministic-key-EXEMPT, timeout=0/retries=0 task -- SAQ's per-queue key
dedup does not apply here) with no server-side check for an already-RUNNING batch on the same
agent+path. A double submit (a slow first request re-clicked, or a re-submit an hour into a
1-2h scan that "looks stalled") therefore dispatched TWO concurrent, unbounded full SHA-256
archive walks of the same tree on the same agent -- doubling archive I/O for hours and racing
their per-file upserts against each other.

The router-side fix adds a pre-insert `IntegrityError` handler; this partial unique index is the
DURABLE, race-safe guard the check-then-insert alone cannot be: a read-then-insert on the Python
side is a TOCTOU (two concurrent requests can both pass the read before either commits), whereas
the database enforces "at most one RUNNING batch per (agent_id, scan_path)" atomically at insert
time, exactly mirroring the existing `uq_scan_batches_agent_id_live` partial index's pattern for
the LIVE sentinel (one per agent).

Revision ID: 044
Revises: 043
Create Date: 2026-07-22
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None

_CREATE = (
    "CREATE UNIQUE INDEX uq_scan_batches_agent_id_scan_path_running "
    "ON public.scan_batches USING btree (agent_id, scan_path) WHERE (status = 'running')"
)
_DROP = "DROP INDEX uq_scan_batches_agent_id_scan_path_running"

# Pre-index dedupe (the migration-041/phaze-am5p lesson, applied as in migration 043): the very bug
# this index guards against has been LIVE, so an existing database can already hold two-or-more
# RUNNING rows for one (agent_id, scan_path) -- creating the unique index over them would abort the
# upgrade. Fail all but the newest RUNNING row per pair (newest created_at, id tiebreaker -- the
# duplicate most likely to still be a real in-flight scan), stamping completed_at and an
# explanatory error_message so the survivors of the historical double-dispatch read as terminal
# rather than lingering RUNNING forever. Static string-literal DML -- no interpolation.
_DEDUPE_EXISTING = """
UPDATE public.scan_batches sb
SET status = 'failed',
    completed_at = now(),
    error_message = 'migration 044: superseded duplicate RUNNING batch (pre-phaze-1a71 double dispatch); newest RUNNING batch for this agent+path kept'
WHERE sb.status = 'running'
  AND EXISTS (
    SELECT 1
    FROM public.scan_batches newer
    WHERE newer.agent_id = sb.agent_id
      AND newer.scan_path = sb.scan_path
      AND newer.status = 'running'
      AND (newer.created_at, newer.id) > (sb.created_at, sb.id)
  )
"""


def upgrade() -> None:
    """Dedupe pre-existing duplicate RUNNING batches, then add the one-RUNNING-per-(agent, path) guard."""
    op.execute(_DEDUPE_EXISTING)
    op.execute(_CREATE)


def downgrade() -> None:
    """Drop the one-RUNNING-batch-per-(agent, path) guard."""
    op.execute(_DROP)
