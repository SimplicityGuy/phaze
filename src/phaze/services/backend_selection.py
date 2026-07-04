"""The Phase-69 pure `select_backend` selection policy over the Phase-68 `Backend` substrate.

This is the single genuinely-new artifact of Phase 69: the load-bearing routing *policy*,
isolated as a pure, synchronous, fully-typed decision function with NO I/O (no `await`, no DB,
no network probe). The Plan-02 drain builds a once-per-tick `snapshot` (one `BackendSlot` per
resolved backend) and calls this per FIFO candidate file; the function returns the `Backend` to
dispatch to, or `None` to hold the file this tick. It NEVER raises -- `None` feeds the cron
no-op discipline (a clean hold, not a failure).

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

Signature note: attempts live on the file's `cloud_job` row, not on `FileRecord`, so the drain
passes `cloud_attempts` explicitly rather than reading `file.cloud_attempts` (RESEARCH pseudocode
used `file.cloud_attempts`; the real model has no such attribute). The function reads
`file.updated_at` -- for a file parked in `AWAITING_CLOUD` this is its entry timestamp (no writer
touches a parked row), so `now - file.updated_at` is exactly its wait duration (RESEARCH Q2).

Local detection is `isinstance(slot["backend"], LocalBackend)`, NOT a rank-99 literal
(RESEARCH Open Q4): rank is operator-tunable config; the class identity is the ground truth.
`backends.py` does not import this module, so the runtime import below introduces no cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import structlog

from phaze.services.backends import LocalBackend


if TYPE_CHECKING:
    from datetime import datetime

    from phaze.config import ControlSettings
    from phaze.models.file import FileRecord
    from phaze.services.backends import Backend


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


def _utilization(slot: BackendSlot) -> float:
    """in_flight/cap utilization for the SCHED-04 tie-break; 0.0 when cap is 0 (never divides by zero)."""
    cap = slot["cap"]
    if cap <= 0:
        return 0.0
    return (cap - slot["remaining"]) / cap


def select_backend(
    file: FileRecord,
    cloud_attempts: int,
    snapshot: dict[str, BackendSlot],
    now: datetime,
    cfg: ControlSettings,
) -> Backend | None:
    """Return the backend to dispatch `file` to this tick, or `None` to hold it (never raises).

    Pure and synchronous -- reads only `file.updated_at`, the in-memory `snapshot`, `cloud_attempts`,
    and the two bounded config knobs. Encodes SCHED-01/04 + D-01/D-03/D-04/D-06 (see module docstring).
    """
    # 1. Eligible = available AND has a free slot.
    eligible = [slot for slot in snapshot.values() if slot["available"] and slot["remaining"] > 0]

    # 2. Attempt-exclusion (D-04): a file that has spent its cloud budget is cloud/Kueue-INELIGIBLE.
    #    Local is never excluded -- it is the guaranteed safety net.
    attempts_exhausted = cloud_attempts >= cfg.cloud_submit_max_attempts
    if attempts_exhausted:
        eligible = [slot for slot in eligible if isinstance(slot["backend"], LocalBackend)]

    # 3. Staleness gate on local spill (D-01/D-03). Local is eligible ONLY when:
    #      (a) every non-local backend is OFFLINE (spill immediately -- NOT staleness-gated), OR
    #      (b) the file has waited past the threshold (spill after a transient full window), OR
    #      (c) the file already exhausted its cloud budget (step 2 forced local).
    #    `any_non_local_online` keys off `available` (online-ness), distinguishing "cloud OFFLINE"
    #    (-> local now) from "cloud online but FULL" (-> local gated behind the wait threshold).
    any_non_local_online = any(slot["available"] for slot in snapshot.values() if not isinstance(slot["backend"], LocalBackend))
    waited = (now - file.updated_at).total_seconds() >= cfg.cloud_spill_to_local_after_seconds
    local_ok = (not any_non_local_online) or waited or attempts_exhausted
    if not local_ok:
        eligible = [slot for slot in eligible if not isinstance(slot["backend"], LocalBackend)]

    # 4. Hold when nothing is eligible (clean no-op -- the file stays AWAITING_CLOUD this tick).
    if not eligible:
        return None

    # 5. Rank-first, tie-break by utilization then stable id (SCHED-04).
    eligible.sort(key=lambda slot: (slot["backend"].rank, _utilization(slot), slot["backend"].id))
    return eligible[0]["backend"]
