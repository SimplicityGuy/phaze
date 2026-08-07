"""The durable cloud-budget ledger: its ONE writer, and the pure policy that reads it (phaze-2mwyo).

Two halves, deliberately split:

* :func:`record_cloud_budget_spent` -- the ONLY writer of the ``cloud_budget`` table. Called from
  ``services/backends.hold_awaiting_cloud`` (itself the single go-forward writer of
  ``cloud_job.status='awaiting'``, D-01/D-02) at the exact edge where a chain's ``attempts`` first
  reaches ``cloud_submit_max_attempts``. NEVER commits -- the caller owns the transaction boundary,
  same discipline as ``hold_awaiting_cloud`` itself.
* :func:`cloud_budget_hold_reason` -- a PURE, synchronous, I/O-free policy over that record plus three
  ``ControlSettings`` knobs, consumed by ``services/backend_selection.select_backend_with_reason``. It
  answers one question: "may this file be offered to a cloud backend right now?"

WHY EVIDENCE IN THE TABLE AND POLICY IN CONFIG. ``phaze-6ck1`` has not established why ~4 files in ~520
grow one ``analysis_child`` past 8 GiB. If that turns out to be INTRINSIC to those files, the right
answer is a permanent cloud-ineligible marker; if it turns out to be ENVIRONMENTAL (the node that died),
the right answer is a cooldown. Nobody can pick yet, and the fix cannot wait for the answer. So the table
stores only what happened and this module decides what it means -- which makes the eventual answer a
config default, not a second migration. That is the bead's explicit "design so the policy can change
without another migration" requirement, discharged here.

THE POLICY SHIPPED, AND WHY IT LEANS COOLDOWN-FIRST.

* **Too permissive reproduces the incident**: unbounded fresh chains, eight pods over five days, a burst
  node taken down on each.
* **Too strict permanently exiles a file from the burst path over one bad node** -- and the affected
  files are exactly the large/slow ones that most NEED cloud, since local is the slow lane by definition.

A cooldown ("no cloud for N days after a chain burns out") is the shape that fails safely in both
directions: it rate-limits chains hard enough that a runaway cannot recur on a four-day gap (the observed
re-chain interval was FOUR days; the default is fourteen), and it is **self-clearing**, so a file grounded
by a transient infrastructure fault flies again on its own. That self-clearing property also matters
structurally: unlike ``HOLD_CLOUD_ATTEMPTS_EXHAUSTED`` -- which ``phaze-9sqa`` documents as PERMANENT
while local is full, and which is what turned 14 files into head-of-line poison -- a cooldown hold is
guaranteed to end, so it can never become a new permanent-poison class.

The two lifetime ceilings sit BEHIND the cooldown as the intrinsic-cause backstop: a file that has burned
``cloud_budget_max_chains`` whole chains (default 3, i.e. ~6 weeks of evidence at the default cooldown)
has demonstrated something that is not transient, and stops being offered cloud. They are DEFAULT-ON but
liftable: raising the knob and restarting re-admits the file, with no row edited and no migration -- the
"clearable marker" option, expressed as configuration.

NOTHING HERE CAN STRAND A FILE. Every verdict this module returns bars only CLOUD backends. Local is
never excluded (``select_backend``'s D-04 comment: local is the guaranteed safety net), so a
budget-grounded file is still routed every tick and still reaches a terminal analyze outcome. The
constraint "never leave a row in a state no writer can advance" is satisfied structurally, not by
convention.

ADR-0005 CONTEXT. With the pod memory limit now emitted, a runaway is a pod-scoped OOMKill rather than a
node crash, so the node-loss path these budgets guard should become markedly rarer. That is a reason to
keep the node-loss ceiling generous and separate -- not a reason to leave it unbounded, which is exactly
the mistake ``phaze-1q4g`` had to correct one level down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.models.cloud_budget import CloudBudget


if TYPE_CHECKING:
    from datetime import datetime
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


# phaze-2mwyo: the durable-budget half of the ``select_backend`` hold vocabulary (the per-chain half
# lives in ``services/backend_selection.py``). They live HERE because this module computes them and
# ``backend_selection`` imports this module, never the reverse. Plain module constants, exactly like
# their phaze-9sqa siblings: these are log values, never persisted and never compared across a wire.
HOLD_CLOUD_BUDGET_COOLDOWN = "cloud_budget_cooldown"
"""The file's last cloud chain burned out too recently -- cloud is barred until the cooldown expires.

SELF-CLEARING, and that is the point. Unlike ``HOLD_CLOUD_ATTEMPTS_EXHAUSTED`` (permanent while local is
full -- phaze-9sqa's head-of-line poison), this hold ends on its own at
``budget_spent_at + cloud_budget_cooldown_days``, so a file grounded by a dead node is re-admitted to the
burst path without operator action.
"""

HOLD_CLOUD_BUDGET_CHAINS = "cloud_budget_chains_exhausted"
"""The file has burned ``cloud_budget_max_chains`` whole cloud chains -- the intrinsic-cause backstop.

Persistent, but NOT permanent in the schema sense: it is a comparison against a config knob, so raising
``cloud_budget_max_chains`` (or setting it to 0 to disable the ceiling) re-admits the file with no row
edited and no migration. Local remains eligible throughout.
"""

HOLD_CLOUD_BUDGET_NODE_LOSS = "cloud_budget_node_loss_exhausted"
"""The file has cost ``cloud_budget_max_node_loss`` node-loss re-drives across all its chains.

Kept SEPARATE from :data:`HOLD_CLOUD_BUDGET_CHAINS` on purpose (phaze-1q4g's discipline, one level up):
"this file keeps failing analysis" and "this file keeps taking nodes down with it" are different
situations that want different operator responses, so they must not collapse into one label.
"""


class CloudBudgetState(NamedTuple):
    """The durable ledger as the pure policy sees it -- three tallies and the cooldown clock.

    A plain value object rather than the ORM row so ``select_backend`` stays free of DB entities and the
    drain can build it straight from its per-candidate SELECT. ``None`` in place of one of these (the
    ``cloud_budget`` param of :func:`cloud_budget_hold_reason`) means "no durable row" -- this file has
    never spent a cloud budget, so no durable rule applies to it.
    """

    chains_spent: int
    attempts_spent: int
    node_loss_spent: int
    budget_spent_at: datetime


def cloud_budget_hold_reason(budget: CloudBudgetState | None, now: datetime, cfg: ControlSettings) -> str:
    """Return the HOLD_* label barring this file from cloud right now, or ``""`` if cloud is allowed.

    PURE and synchronous -- no DB, no clock read (``now`` is injected), no raise. Mirrors
    ``select_backend_with_reason``'s ``""``-means-allowed convention so the caller can bin the result
    without a narrowing dance.

    Order is most-specific-first: the two lifetime ceilings are evaluated before the cooldown, because a
    file past a ceiling is barred whether or not its cooldown has expired, and reporting the expired
    cooldown instead would tell an operator to wait for something that will never help. Each ceiling is
    disabled by setting its knob to 0, which is what makes "cooldown only" and "no durable rule at all"
    reachable configurations rather than code changes.

    A ``None`` budget (no durable row -- the overwhelmingly common case) returns ``""`` immediately: this
    rule set only ever constrains a file that has already burned at least one whole cloud chain.
    """
    if budget is None:
        return ""
    # Lifetime ceilings (0 disables). These are the intrinsic-cause backstop; see the module docstring on
    # why they are default-ON but deliberately liftable by config rather than encoded in the schema.
    if cfg.cloud_budget_max_chains and budget.chains_spent >= cfg.cloud_budget_max_chains:
        return HOLD_CLOUD_BUDGET_CHAINS
    if cfg.cloud_budget_max_node_loss and budget.node_loss_spent >= cfg.cloud_budget_max_node_loss:
        return HOLD_CLOUD_BUDGET_NODE_LOSS
    # Cooldown (0 disables). The primary policy: rate-limit chains instead of banning files.
    cooldown_sec = cfg.cloud_budget_cooldown_days * 86_400
    if cooldown_sec <= 0:
        return ""
    spent_at = budget.budget_spent_at
    # models/base.py declares every timestamp tz-aware (phaze-cz3m) and migration 049 made the schema
    # match, but the drain deliberately coerces `now` to the CANDIDATE's awareness (a create_all-built
    # test schema can still hand back naive values), so normalize both sides here rather than trust the
    # caller. A naive-minus-aware subtraction raises TypeError, which in the drain would abort a whole
    # tick -- the same class of failure the drain's own assume-UTC coercion exists to prevent.
    if spent_at.tzinfo is None and now.tzinfo is not None:
        spent_at = spent_at.replace(tzinfo=now.tzinfo)
    elif spent_at.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=spent_at.tzinfo)
    if (now - spent_at).total_seconds() < cooldown_sec:
        return HOLD_CLOUD_BUDGET_COOLDOWN
    return ""


async def record_cloud_budget_spent(session: AsyncSession, file_id: uuid.UUID, *, attempts: int, node_loss_redrives: int) -> None:
    """Fold ONE terminated cloud chain into the file's durable ledger. NEVER commits.

    Additive upsert keyed on ``file_id``: INSERT the first chain, or accumulate onto the existing row.
    ``budget_spent_at`` is re-stamped server-side (``func.now()``) on every fold, so the cooldown clock
    always measures from the MOST RECENT burnout -- a file that burns a second chain restarts its
    cooldown rather than inheriting an already-expired one.

    ``attempts`` and ``node_loss_redrives`` accumulate into SEPARATE columns and are never summed
    together: ``phaze-1q4g`` split them on the ``cloud_job`` row so "failed analysis three times" and
    "lost a node once" stay distinguishable, and that distinction is worth more, not less, once it is the
    durable record an operator reads weeks later.

    EXACTLY-ONCE PER CHAIN is the caller's job, not this function's -- ``hold_awaiting_cloud`` calls it
    only on the rising edge where the row's ``attempts`` first crosses ``cloud_submit_max_attempts``, so
    a repeated/late spill CAS against an already-spent row folds nothing. This function is deliberately
    NOT idempotent: it is an accumulator, and making it idempotent would require it to guess which chain
    it was being told about.

    NEVER commits (``hold_awaiting_cloud``'s contract, inherited): a commit here would drop the drain
    tick's ``pg_advisory_xact_lock`` and re-open the over-stage class (Landmine L1).
    """
    now = func.now()
    stmt = pg_insert(CloudBudget).values(
        file_id=file_id,
        chains_spent=1,
        attempts_spent=attempts,
        node_loss_spent=node_loss_redrives,
        budget_spent_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        # file_id is the PK, so this is the natural conflict target -- no separate unique index.
        index_elements=["file_id"],
        set_={
            # Column references on the SET side render as the EXISTING row's values, so these are true
            # accumulations (excluded.* would instead re-write this chain's contribution alone).
            "chains_spent": CloudBudget.chains_spent + 1,
            "attempts_spent": CloudBudget.attempts_spent + attempts,
            "node_loss_spent": CloudBudget.node_loss_spent + node_loss_redrives,
            "budget_spent_at": now,
            # TimestampMixin's ORM-level onupdate never fires on a Core ON CONFLICT DO UPDATE (phaze-c8nz);
            # stamp it explicitly so a second fold moves updated_at instead of freezing it at first write.
            "updated_at": now,
        },
    )
    await session.execute(stmt)
