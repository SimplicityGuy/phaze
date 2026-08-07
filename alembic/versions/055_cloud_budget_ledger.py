"""Add the ``cloud_budget`` table -- the durable per-file cloud budget that outlives the sidecar (phaze-2mwyo).

Migration 054 gave the node-loss re-drive path its own bounded counter, which bounds ONE cloud chain.
It could not bound the NUMBER of chains, because every counter it bounds lives on the ``cloud_job``
sidecar -- and ``routers/agent_analysis``'s D-14 reaper legitimately DELETEs that row
(``DELETE FROM cloud_job WHERE file_id = ... AND status = 'awaiting'``) on BOTH analyze-terminal seams.
A file that spent its whole cloud budget, spilled to local, and then failed LOCALLY therefore lost its
entire budget history, and the next re-analysis started a fresh chain at ``attempts = 0``. That is what
the phaze-wcrb forensics actually recorded: cloud job ``713a368e``'s eight pods are TWO chains of four,
2026-07-24..07-25 and 2026-07-29, with a four-day gap -- not one runaway chain of eight.

Narrowing the reaper's WHERE clause is the obvious fix and is wrong: retaining budget-spent ``awaiting``
rows re-creates exactly the monotonically-growing dead set that ``ix_cloud_job_awaiting`` scans and that
phaze-9sqa's head-of-line starvation came from. The reaper is doing a necessary job. So the budget moves
to a row the reaper has no reason to touch.

WHAT THIS TABLE IS NOT: it is not a "banned" flag. Every column is evidence -- how many chains burned
out, how much ANALYZE budget and how much NODE-LOSS budget they cost (kept apart, per 054's discipline),
and when the most recent one ended. The verdict lives entirely in ``ControlSettings``
(``cloud_budget_cooldown_days`` / ``cloud_budget_max_chains`` / ``cloud_budget_max_node_loss``) and is
computed by the pure ``services/cloud_budget.cloud_budget_hold_reason``. phaze-6ck1 has not yet
established whether the ~4 runaway files are intrinsically pathological or the victims of one bad node,
so the policy must be able to change when it does -- and changing it changes a default, not this schema.

BACKFILL. Every ``cloud_job`` row currently parked at ``status='awaiting'`` with ``attempts > 0`` is
recorded as ONE spent chain, dated at that row's ``updated_at`` (its spill moment). Rationale: an
awaiting row lands at ``attempts = 0`` (a fresh hold) or at the cap (a terminal spill), so ``> 0`` is
"this chain charged the file at least one cloud attempt and is now parked" -- the closest reconstruction
available without reading a config value inside DDL. It is over-inclusive by exactly one case
(``KueueBackend._reap_stranded_staging`` spills at ``min(attempts + 1, cap)``, which can be BELOW the
cap), and that over-inclusion is cheap and self-limiting: the file is credited one chain out of three and
a cooldown measured from an already-old ``updated_at``, so most such rows are out of cooldown before this
migration finishes. Under-inclusion would be the expensive direction -- it would hand the four known
incident files one more full cloud chain the moment the reaper next deletes their sidecar row.

``downgrade`` drops the table, losing only the cross-chain history; ``cloud_job``'s own per-chain
counters are untouched by this migration in either direction.

Revision ID: 055
Revises: 054
Create Date: 2026-08-06
"""

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None

_TABLE = "cloud_budget"


def upgrade() -> None:
    """Create the durable per-file cloud-budget ledger and seed it from budget-spent awaiting rows."""
    op.create_table(
        _TABLE,
        # file_id IS the PK: one durable budget row per file, no surrogate key. ON DELETE CASCADE so this
        # sidecar can never block a scan deletion (stage_skip's lesson -- a file sidecar with neither a
        # cascade nor an explicit cleanup makes a batch permanently undeletable).
        sa.Column("file_id", sa.dialects.postgresql.UUID(as_uuid=True), sa.ForeignKey("files.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("chains_spent", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempts_spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("node_loss_spent", sa.Integer(), nullable=False, server_default="0"),
        # NOT NULL: the row exists only because a budget was spent, so there is no "nothing spent yet"
        # state to represent -- absence of the row IS that state. timestamptz per migration 049.
        sa.Column("budget_spent_at", sa.DateTime(timezone=True), nullable=False),
        # NOT NULL + timestamptz, matching TimestampMixin's `Mapped[datetime]` declaration exactly
        # (migration 050's precedent). Omitting NOT NULL is what left discogs_links/tag_write_log in the
        # frozen ORM<->schema drift set; a NEW table has no excuse to join them.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("chains_spent >= 0", name="ck_cloud_budget_chains_spent_nonneg"),
        sa.CheckConstraint("attempts_spent >= 0", name="ck_cloud_budget_attempts_spent_nonneg"),
        sa.CheckConstraint("node_loss_spent >= 0", name="ck_cloud_budget_node_loss_spent_nonneg"),
    )
    # Backfill (see the module docstring for why `attempts > 0` and why over-inclusion is the safe
    # direction). ON CONFLICT DO NOTHING is belt-and-braces: cloud_job.file_id is already unique, so this
    # SELECT cannot produce two rows for one file.
    op.execute(
        sa.text(
            """
            INSERT INTO cloud_budget (file_id, chains_spent, attempts_spent, node_loss_spent, budget_spent_at, created_at, updated_at)
            SELECT file_id, 1, attempts, node_loss_redrives, COALESCE(updated_at, created_at, now()), now(), now()
            FROM cloud_job
            WHERE status = 'awaiting' AND attempts > 0
            ON CONFLICT (file_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    """Drop the ledger. Loses the cross-chain history only; cloud_job's per-chain counters are untouched."""
    op.drop_table(_TABLE)
