"""CloudBudget model -- the DURABLE per-file cloud-budget ledger that outlives the sidecar (phaze-2mwyo).

WHY THIS TABLE EXISTS AT ALL. The file's cloud retry budget used to live in exactly one place:
``cloud_job.attempts``. That row is a **sidecar for one burst**, and ``routers/agent_analysis``'s D-14
reaper legitimately DELETES it (``DELETE FROM cloud_job WHERE file_id = ... AND status = 'awaiting'``) on
BOTH analyze-terminal seams -- the result PUT and the failure POST -- because ``ix_cloud_job_awaiting``
would otherwise scan a monotonically growing dead set at 200K rows (phaze-9sqa's head-of-line poison, the
reason the drain had to be paginated in the first place). So a file that spent its whole cloud budget,
spilled to local, and then failed LOCALLY lost its entire budget history: the next re-analysis started a
fresh chain at ``attempts = 0`` and was free to spend the full budget again, and again, forever.

That is not theoretical. Spike ``phaze-wcrb`` recorded cloud job ``713a368e`` producing EIGHT pods over
five days against a ceiling of three; ``phaze-1q4g`` then showed those eight pods are **two chains of
four** (2026-07-24 -> 07-25, then a fresh chain on 07-29 after a four-day gap), not one runaway chain.
``phaze-1q4g`` bounded a single chain. THIS table is what bounds the number of chains.

THE FIX IS NOT A NARROWER REAPER. Retaining budget-spent ``awaiting`` rows so the counter survives is the
obvious change and it is wrong -- it re-creates precisely the dead-set growth the reaper exists to
prevent. The reaper is doing a necessary job; the defect was that the budget was stored somewhere the
reaper is entitled to delete. So the budget moves OUT of the ephemeral sidecar and into this durable
per-file row, which nothing in the reap path touches. ``cloud_job`` keeps its per-chain counters
unchanged (a chain still bounds itself); this table accumulates them ACROSS chains.

WHAT IS RECORDED -- EVIDENCE, NOT A VERDICT. Every column here is a fact about what happened. No column
says "this file is banned". That separation is deliberate and load-bearing: ``phaze-6ck1`` has NOT yet
established why ~4 files in ~520 grow one ``analysis_child`` past 8 GiB, so nobody can yet say whether
the right policy is a cooldown (environmental cause -- the node that died) or a permanent exile
(intrinsic to the file). Storing evidence and deciding policy in config
(``services/cloud_budget.py``'s pure :func:`cloud_budget_hold_reason` over three ``ControlSettings``
knobs) means the answer to that question changes a default, not the schema. **The policy can change
without another migration.**

TWO CAUSES, STILL SEPARATELY LEGIBLE. ``phaze-1q4g`` deliberately kept ``cloud_job.attempts`` (the
file's ANALYZE retry budget) and ``cloud_job.node_loss_redrives`` (re-drives taken because the pod died
WITH ITS NODE -- an infrastructure fault that must not spend the file's analyze budget) as separate
counters, so a row could say which of the two happened. This table preserves that distinction one level
up: ``attempts_spent`` and ``node_loss_spent`` accumulate separately and are capped separately, so a file
held off cloud after repeatedly FAILING ANALYSIS and one held off after repeatedly LOSING A NODE remain
different, readable situations rather than one anonymous "bad file" flag.

ROW EXISTENCE IS THE MARKER. There is no row until a chain terminates with its budget spent, so
``budget_spent_at`` is NOT NULL: a row means "at least one cloud chain has ended at a ceiling", and no row
means "this file has never spent a cloud budget". The single writer is
``services/cloud_budget.record_cloud_budget_spent``, called from ``services/backends.hold_awaiting_cloud``
-- the one go-forward writer of ``cloud_job.status='awaiting'`` (D-01/D-02) -- on the edge where a chain
first reaches ``cloud_submit_max_attempts``. Fold-once is enforced by that edge trigger, not by this
table.

NOTHING HERE CAN STRAND A FILE. Local (rank-99) is never excluded by any budget rule -- it is the
guaranteed safety net -- so a cloud-ineligible file is still routable every tick, and the cooldown
policy is self-clearing by construction. The hard chain/node-loss ceilings are config values an operator
can raise with a restart, so even the strictest verdict this record can produce is reversible without
touching a row.

``chains_spent`` is NOT derivable from ``attempts_spent / cloud_submit_max_attempts``: the cap is an
operator-tunable knob, so two chains fought under different caps contribute different amounts. It is
stored because a future chain-count policy must not need a migration either.
"""

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class CloudBudget(TimestampMixin, Base):
    """One row per file recording the cloud budget it has spent ACROSS bursts (phaze-2mwyo).

    Written only when a cloud chain terminates at a ceiling; absent otherwise. Deliberately NOT a
    column set on ``files``: Phase 90 (MIG-04) removed ``files.state`` precisely so the central file
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
    # bounded, and by its own lifetime ceiling. ADR-0005's pod memory limit should make this path rare:
    # a runaway now OOMKills its own pod instead of taking the node down under it.
    node_loss_spent: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    # When the MOST RECENT chain terminated. NOT NULL: the row only exists because a budget was spent, so
    # there is no "spent nothing yet" state to represent. This is the cooldown clock the policy reads.
    # timezone=True per TimestampMixin's phaze-cz3m invariant (no naive timestamp column anywhere).
    budget_spent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # The three counters are monotonic tallies -- a negative value is a writer bug, not a state. Bare
        # names; the ck_%(table_name)s_%(constraint_name)s convention re-prefixes them.
        CheckConstraint("chains_spent >= 0", name="chains_spent_nonneg"),
        CheckConstraint("attempts_spent >= 0", name="attempts_spent_nonneg"),
        CheckConstraint("node_loss_spent >= 0", name="node_loss_spent_nonneg"),
    )
