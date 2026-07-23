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


def upgrade() -> None:
    """Add the one-RUNNING-batch-per-(agent, path) guard."""
    op.execute(_CREATE)


def downgrade() -> None:
    """Drop the one-RUNNING-batch-per-(agent, path) guard."""
    op.execute(_DROP)
