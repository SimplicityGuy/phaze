"""The Phase-69 pure `select_backend` selection policy over the Phase-68 `Backend` substrate.

This is the single genuinely-new artifact of Phase 69: the load-bearing routing *policy*,
isolated as a pure, synchronous, fully-typed decision function with NO I/O (no `await`, no DB,
no network probe). The Plan-02 drain builds a once-per-tick `snapshot` (one `BackendSlot` per
resolved backend) and calls this per FIFO candidate file; the function returns the `Backend` to
dispatch to, or `None` to hold the file this tick. It NEVER raises -- `None` feeds the cron
no-op discipline (a clean hold, not a failure).

phaze-9sqa adds `select_backend_with_reason`: the SAME policy, returning `(backend, None)` or
`(None, HOLD_*)`. `select_backend` is now a thin wrapper that drops the label. The label exists because a
bare `None` cannot distinguish a self-clearing hold (everything busy, the spill clock still ticking) from a
permanent one (the D-04 attempts cap with local full) -- the distinction between a lane that is idle and a
lane that is starving, which the drain needs in order to alarm on the second.

The policy encodes (RESEARCH § "Pattern 2" + § "Novel Mechanism 3"):

* **SCHED-01** rank-first eligible dispatch -- the available lowest-rank backend with a free slot
  wins; a full lowest-rank backend spills to the next rank, per candidate.
* **D-01 / D-03** the staleness gate on local spill -- when higher-rank backends are online-but-FULL
  the slow local (rank-99) backend is eligible only after the file has waited past
  `cloud_spill_to_local_after_seconds`; but when every non-local backend is OFFLINE, local is
  eligible immediately (the guard gates the full->local path, NOT the offline->local path).
* **D-04** attempt-exclusion -- a file whose cloud attempt count has reached
  `cloud_submit_max_attempts` is excluded from cloud/Kueue candidates and routes to local only
  (the anti-thrash bound; local is never excluded -- it is the guaranteed safety net).
* **D-06** stateless re-rank -- the function carries NO per-file "last-failed backend" memory; a
  failed backend may be re-picked, bounded only by the D-04 attempt-count eligibility filter. The
  exclusion derives from a *counter* (`cloud_attempts`), never a remembered backend id.
* **SCHED-04** tie-break -- equal-rank backends are ordered by in_flight/cap utilization, then by
  stable (lexicographic) id.

phaze-2mwyo adds the DURABLE half of D-04. `cloud_attempts` bounds ONE chain, and it cannot bound more
than one: it is read off the `cloud_job` sidecar, which `routers/agent_analysis`'s D-14 reaper
legitimately DELETES on both analyze-terminal seams -- so a file that spent its budget, spilled to local
and then failed locally re-entered this function with `cloud_attempts = 0` and a whole fresh budget (two
chains of four pods, four days apart, is what the phaze-wcrb forensics actually recorded). The optional
`cloud_budget` argument carries the per-file record that OUTLIVES the sidecar
(`services/cloud_budget.py`), and its verdict enters the policy at exactly the same place and with
exactly the same effect as the attempts cap: cloud/Kueue backends are filtered out, local is not. The
difference is the LABEL -- a durable hold reports why, and (for the cooldown) that it will clear itself,
which `HOLD_CLOUD_ATTEMPTS_EXHAUSTED` explicitly does not.

Signature note: attempts live on the file's `cloud_job` row, not on `FileRecord`, so the drain
passes `cloud_attempts` explicitly rather than reading `file.cloud_attempts` (RESEARCH pseudocode
used `file.cloud_attempts`; the real model has no such attribute). The staleness clock is likewise
passed explicitly as `lane_entered_at` -- Phase 83 (D-07) moved it from `file.updated_at` to the
awaiting `cloud_job.updated_at`, because Phase 90 removes the dual-written `file.state` (and thus the
`file.updated_at` lane-entry stamp), which would otherwise silently break `now - lane_entered_at` as
the wait duration. FIFO ordering stays on the immutable `FileRecord.created_at` in the drain query.

Local detection is `isinstance(slot["backend"], LocalBackend)`, NOT a rank-99 literal
(RESEARCH Open Q4): rank is operator-tunable config; the class identity is the ground truth.
`backends.py` does not import this module, so the runtime import below introduces no cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import structlog

from phaze.services.backends import LocalBackend
from phaze.services.cloud_budget import cloud_budget_hold_reason


if TYPE_CHECKING:
    from datetime import datetime

    from phaze.config import ControlSettings
    from phaze.services.backends import Backend
    from phaze.services.cloud_budget import CloudBudgetState


logger = structlog.get_logger(__name__)


class BackendSlot(TypedDict):
    """One resolved backend's once-per-tick state, built by the drain and consumed by selection.

    `remaining` is `cap - in_flight_count` at snapshot time; `available` is the backend's
    `is_available()` probe result. Probed ONCE per tick -- never re-probed inside the candidate
    loop (the snapshot is the atomicity boundary).
    """

    backend: Backend
    available: bool
    remaining: int
    cap: int


# phaze-9sqa: the closed vocabulary of "why was this candidate held this tick?", surfaced by
# :func:`select_backend_with_reason` and aggregated by the drain into the repeated-all-held WARNING.
# Before this, a hold was an unlabelled `None` and the drain's only trace was `skipped: N` -- which
# reads identically whether the lane is healthy-and-idle or starving behind poisoned heads, and so hid
# a >24h production stall in plain sight. Plain module constants (not a StrEnum): these are log values,
# never persisted, never compared across a wire.
HOLD_NO_FREE_SLOTS = "no_free_slots"
"""Every backend is unavailable or already at cap -- the whole registry is saturated, not this file."""

HOLD_CLOUD_ATTEMPTS_EXHAUSTED = "cloud_attempts_exhausted"
"""D-04: the file spent its cloud budget, so only local could take it -- and local had no free slot.

The production phaze-9sqa shape. It is a PERMANENT hold for as long as local is full: `cloud_attempts`
only ever grows, so the file cannot become cloud-eligible again by waiting. That is what made these
files head-of-line poison rather than a transient skip.
"""

HOLD_LOCAL_SPILL_NOT_REACHED = "local_spill_not_reached"
"""D-01/D-03: local was the only backend with a slot, but the file has not waited out the spill gate.

Self-clearing -- the file becomes eligible once `cloud_spill_to_local_after_seconds` elapses.
"""

# phaze-2mwyo: the DURABLE (cross-chain) hold labels -- HOLD_CLOUD_BUDGET_COOLDOWN /
# HOLD_CLOUD_BUDGET_CHAINS / HOLD_CLOUD_BUDGET_NODE_LOSS -- are defined in `services/cloud_budget.py`,
# next to the pure policy that emits them, because THIS module imports that one and not the reverse.
# They are the same closed vocabulary the drain bins and logs; see that module for what each means and
# which of them clear themselves.


def _utilization(slot: BackendSlot) -> float:
    """in_flight/cap utilization for the SCHED-04 tie-break; 0.0 when cap is 0 (never divides by zero)."""
    cap = slot["cap"]
    if cap <= 0:
        return 0.0
    return (cap - slot["remaining"]) / cap


def select_backend(
    lane_entered_at: datetime,
    cloud_attempts: int,
    snapshot: dict[str, BackendSlot],
    now: datetime,
    cfg: ControlSettings,
    *,
    cloud_budget: CloudBudgetState | None = None,
) -> Backend | None:
    """Return the backend to dispatch this candidate to this tick, or `None` to hold it (never raises).

    Pure and synchronous -- reads only `lane_entered_at` (the awaiting `cloud_job.updated_at` staleness
    clock, D-07), the in-memory `snapshot`, `cloud_attempts`, the optional durable `cloud_budget` record,
    and the bounded config knobs. Encodes SCHED-01/04 + D-01/D-03/D-04/D-06 (see module docstring).

    The one-value face of :func:`select_backend_with_reason`, which carries the identical policy plus the
    label for WHY a hold happened. Kept as the primary name because every caller that only routes (and
    every policy test) wants exactly this.
    """
    return select_backend_with_reason(lane_entered_at, cloud_attempts, snapshot, now, cfg, cloud_budget=cloud_budget)[0]


def select_backend_with_reason(
    lane_entered_at: datetime,
    cloud_attempts: int,
    snapshot: dict[str, BackendSlot],
    now: datetime,
    cfg: ControlSettings,
    *,
    cloud_budget: CloudBudgetState | None = None,
) -> tuple[Backend | None, str]:
    """Return `(backend, "")` on a dispatch, or `(None, reason)` on a hold (phaze-9sqa; never raises).

    THE policy -- :func:`select_backend` delegates here and drops the label. The second element is `""`
    exactly when a backend was selected (a plain `str` rather than `str | None` so the drain can bin it
    without a narrowing dance the type system cannot do across an unpacked tuple). On a hold it is one of
    the `HOLD_*` constants above, naming which filter emptied the eligible set -- which is the difference
    between "the lane is idle because everything is busy" (`HOLD_NO_FREE_SLOTS`, self-clearing) and "the
    lane is starving because its oldest rows can never be routed" (`HOLD_CLOUD_ATTEMPTS_EXHAUSTED`,
    permanent while local is full). The drain aggregates these per tick and escalates to WARNING when
    consecutive ticks hold 100% of what they scanned.

    Behavior is byte-identical to the pre-label policy: the same filters in the same order, the same
    `None` for the same inputs. Each filter now returns its own labelled empty-set exit instead of falling
    through to one shared `if not eligible` at the end -- an equivalent short-circuit, since a filter that
    empties the list can never be refilled by a later one.

    phaze-2mwyo: `cloud_budget` is the DURABLE per-file record (`services/cloud_budget.CloudBudgetState`)
    of budgets spent in PREVIOUS chains -- the history the D-14 reaper deletes along with the sidecar row.
    It defaults to `None`, which means "no durable row, so no durable rule": that is the correct reading
    for a file that has never burned a cloud chain, and it is also what keeps every routing-only caller
    and policy test that does not model the ledger unchanged. The drain (`tasks/release_awaiting_cloud`)
    is the ONE production caller, and it always supplies it.
    """
    # 1. Eligible = available AND has a free slot.
    eligible = [slot for slot in snapshot.values() if slot["available"] and slot["remaining"] > 0]
    if not eligible:
        # Registry-wide saturation, not a property of this file: every backend is down or at cap.
        return None, HOLD_NO_FREE_SLOTS

    # 2. Budget-exclusion (D-04 + phaze-2mwyo): a file that has spent its cloud budget -- in THIS chain
    #    (`cloud_attempts`, the sidecar counter) or in PREVIOUS ones (`cloud_budget`, the durable record
    #    the D-14 reaper cannot delete) -- is cloud/Kueue-INELIGIBLE. Local is never excluded; it is the
    #    guaranteed safety net, which is exactly why no verdict here can strand a file.
    #
    #    The per-chain cap is checked first so its label wins when both apply: a file at the cap right now
    #    is being held by the live chain, and that is the more actionable reading. The two produce the
    #    SAME filter -- only the reported reason differs, and the reason is the whole point (phaze-9sqa),
    #    since `cloud_attempts_exhausted` is permanent while local is full and `cloud_budget_cooldown`
    #    clears itself.
    cloud_barred_reason = (
        HOLD_CLOUD_ATTEMPTS_EXHAUSTED if cloud_attempts >= cfg.cloud_submit_max_attempts else cloud_budget_hold_reason(cloud_budget, now, cfg)
    )
    if cloud_barred_reason:
        eligible = [slot for slot in eligible if isinstance(slot["backend"], LocalBackend)]
        if not eligible:
            # phaze-9sqa's head-of-line poison: cloud-ineligible, and local has nothing free. Permanent
            # for the attempts cap (the counter only grows); bounded for a cooldown, which expires.
            return None, cloud_barred_reason

    # 3. Staleness gate on local spill (D-01/D-03). Local is eligible ONLY when:
    #      (a) every non-local backend is OFFLINE (spill immediately -- NOT staleness-gated), OR
    #      (b) the file has waited past the threshold (spill after a transient full window), OR
    #      (c) the file is barred from cloud entirely (step 2 forced local -- per-chain cap OR durable
    #          budget rule; waiting out a staleness gate cannot help a file no cloud backend may take).
    #    `any_non_local_online` keys off `available` (online-ness), distinguishing "cloud OFFLINE"
    #    (-> local now) from "cloud online but FULL" (-> local gated behind the wait threshold).
    any_non_local_online = any(slot["available"] for slot in snapshot.values() if not isinstance(slot["backend"], LocalBackend))
    waited = (now - lane_entered_at).total_seconds() >= cfg.cloud_spill_to_local_after_seconds
    local_ok = (not any_non_local_online) or waited or bool(cloud_barred_reason)
    if not local_ok:
        eligible = [slot for slot in eligible if not isinstance(slot["backend"], LocalBackend)]
        if not eligible:
            # Only local had a slot and this file has not waited it out yet -- clears itself with time.
            return None, HOLD_LOCAL_SPILL_NOT_REACHED

    # 4. Rank-first, tie-break by utilization then stable id (SCHED-04).
    eligible.sort(key=lambda slot: (slot["backend"].rank, _utilization(slot), slot["backend"].id))
    return eligible[0]["backend"], ""
