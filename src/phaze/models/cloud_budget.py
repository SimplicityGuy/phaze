"""Durable per-file cloud-budget evidence that outlives an ephemeral ``cloud_job`` burst.

Awaiting-job reaping must delete dead sidecars so ``ix_cloud_job_awaiting`` remains bounded at the
200,000-file target. This separate ledger therefore accumulates spent retry chains without retaining
those rows. Attempts and node-loss re-drives remain distinct because an infrastructure failure must not
consume the file's analysis-failure budget. The policy is reversible and configuration-owned: local
execution remains eligible, and operators can change the chain, node-loss, and cooldown limits without
a migration. See ``docs/configuration.md`` for the measured incident and policy rationale.
"""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class CloudBudget(TimestampMixin, Base):
    """One row per file recording the cloud budget it has spent across bursts.

    Written only when a cloud chain terminates at a ceiling; absent otherwise. Deliberately NOT a
    column set on ``files``: the central file
    record carries description, never pipeline/scheduling state. This is scheduling accounting, so it
    lives in its own per-file sidecar (the ``scheduling_ledger`` precedent), keyed 1:1 on ``file_id``.
    """

    __tablename__ = "cloud_budget"

    # file_id IS the primary key -- one durable budget row per file, no surrogate id and no separate
    # unique index needed. ondelete=CASCADE so a scan deletion can never be blocked by this sidecar
    # (services/scan_deletion.py still deletes it explicitly, in child->parent order, for the rowcount
    # report; the cascade is the belt to that suspenders, matching stage_skip's hard-won lesson that a
    # file sidecar with no cascade AND no cleanup makes a batch permanently undeletable).
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), primary_key=True)
    # How many cloud chains have ended at a ceiling for this file. NOT derivable from attempts_spent
    # (cloud_submit_max_attempts is operator-tunable, so chains fought under different caps contribute
    # different amounts) -- stored so a chain-count policy needs no migration.
    chains_spent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    # Cumulative cloud_job.attempts charged across every terminated chain -- the ANALYSIS-FAILURE budget.
    # Kept separate from node_loss_spent so the two causes stay distinguishable (phaze-1q4g's discipline).
    attempts_spent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    # Cumulative cloud_job.node_loss_redrives charged across every terminated chain -- the NODE-LOSS
    # budget. An infra fault is not the file's fault, so it never spends attempts_spent; but it is still
    # bounded, and by its own lifetime ceiling. ``docs/design/0005-analyze-job-memory-limits.md``
    # makes this path rare:
    # a runaway now OOMKills its own pod instead of taking the node down under it.
    node_loss_spent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    # When the MOST RECENT chain terminated. NOT NULL: the row only exists because a budget was spent, so
    # there is no "spent nothing yet" state to represent. This is the cooldown clock the policy reads.
    # timezone=True per TimestampMixin's phaze-cz3m invariant (no naive timestamp column anywhere).
    budget_spent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The three counters are monotonic tallies -- a negative value is a writer bug, not a state. Bare
    # names; the ck_%(table_name)s_%(constraint_name)s convention re-prefixes them.
    __table_args__ = (
        CheckConstraint("chains_spent >= 0", name="chains_spent_nonneg"),
        CheckConstraint("attempts_spent >= 0", name="attempts_spent_nonneg"),
        CheckConstraint("node_loss_spent >= 0", name="node_loss_spent_nonneg"),
    )
