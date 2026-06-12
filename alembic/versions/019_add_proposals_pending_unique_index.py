"""Add a partial unique index enforcing one PENDING proposal per file (D-04).

Phase 35 idempotency migration. ``generate_proposals`` is being converted to an
upsert keyed on ``(file_id) WHERE status = 'pending'`` (services/proposal.py).
That upsert needs a matching partial UNIQUE index as its ON CONFLICT target, and
the index also structurally guarantees "one active proposal per file" at the DB
level -- re-runs overwrite the PENDING row in place and can never accumulate a
second pending row for the same file.

BLOCKING DATA HAZARD (35-RESEARCH Q3): the live 11,428-file archive almost
certainly already has multiple PENDING proposals per file (the pre-Phase-35
``store_proposals`` issued a fresh INSERT on every re-run). ``CREATE UNIQUE
INDEX`` would ABORT on those duplicates. This revision therefore runs TWO ordered
ops in ``upgrade()``:

  1. Collapse existing duplicate PENDING rows to one-per-file, keeping the
     most-recent ``created_at``. This MUST run first or op 2 aborts.
  2. Create the partial unique index ``uq_proposals_file_id_pending``.

APPROVED / EXECUTED / REJECTED / FAILED rows fall OUTSIDE the partial index
(``WHERE status = 'pending'``) so they are never touched by either op -- human
approvals are structurally protected.

The op-1 dedupe DELETE is NOT reversible (the discarded duplicate rows are gone).
``downgrade()`` therefore only drops the index; it cannot resurrect the collapsed
duplicates, which is acceptable -- they were redundant PENDING rows by definition.

Revision ID: 019
Revises: 018
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "019"
down_revision: str | Sequence[str] | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Static, parameterless SQL (no user input interpolated) -- a row_number() window
# over a fixed table; no injection surface (35 threat T-35-04).
_DEDUPE_PENDING_SQL = """
DELETE FROM proposals p
USING (
    SELECT id, row_number() OVER (PARTITION BY file_id ORDER BY created_at DESC) AS rn
    FROM proposals
    WHERE status = 'pending'
) d
WHERE p.id = d.id AND d.rn > 1
"""


def upgrade() -> None:
    """Collapse duplicate PENDING rows, then create the partial unique index.

    Order is load-bearing: op 1 (dedupe) MUST precede op 2 (create unique index)
    or the index build aborts on the live archive's pre-existing duplicates.
    """
    # Op 1: collapse existing duplicate PENDING rows to one-per-file (keep the
    # most-recent created_at). Approved/executed/etc rows are untouched (the
    # WHERE status = 'pending' guard).
    op.execute(sa.text(_DEDUPE_PENDING_SQL))
    # Op 2: partial unique index = the on_conflict_do_update target for
    # store_proposals' D-04 upsert.
    op.create_index(
        "uq_proposals_file_id_pending",
        "proposals",
        ["file_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """Drop the partial unique index.

    The op-1 dedupe is NOT reversible -- the collapsed duplicate PENDING rows are
    permanently gone and cannot be resurrected here.
    """
    op.drop_index("uq_proposals_file_id_pending", table_name="proposals")
