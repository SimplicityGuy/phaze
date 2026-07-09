"""Unit tests for the Phase 69 pure `select_backend` selection policy (SCHED-01/03/04).

`select_backend` is a pure, synchronous, fully-typed decision function -- no DB, no
Redis, no `await`. It takes a once-per-tick backend snapshot (built by the Plan-02
drain), the candidate file, its cloud attempt count, and the config, and returns the
`Backend` to dispatch to (or `None` = "hold this file this tick", never raises).

Cases mirror RESEARCH § "Pattern 2" (rank-first eligible selection) and § "Novel
Mechanism 3" (attempt-exclusion). Test names carry the `rank` / `spill` / `stale` /
`attempt` / `stateless` / `tiebreak` / `hold` keywords so the VALIDATION `-k` filters
bind. Pure -- no DB fixtures required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import inspect
from typing import Any

from phaze.config import ControlSettings
from phaze.services.backend_selection import BackendSlot, select_backend
from phaze.services.backends import ComputeAgentBackend, KueueBackend, LocalBackend


NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


# --- construction helpers ----------------------------------------------------------------


def _local(*, id: str = "local", rank: int = 99, cap: int = 10) -> LocalBackend:
    """A LocalBackend with a positive cap so the drain-supplied `remaining` can be > 0."""
    return LocalBackend(id=id, rank=rank, cap=cap)


def _compute(*, id: str = "compute-a1", rank: int = 10, cap: int = 2) -> ComputeAgentBackend:
    return ComputeAgentBackend(id=id, rank=rank, cap=cap)


def _kueue(*, id: str = "kueue-x64", rank: int = 20, cap: int = 5) -> KueueBackend:
    return KueueBackend(id=id, rank=rank, cap=cap)


def _slot(backend: Any, *, available: bool = True, remaining: int = 1, cap: int | None = None) -> BackendSlot:
    return {
        "backend": backend,
        "available": available,
        "remaining": remaining,
        "cap": backend.cap if cap is None else cap,
    }


def _snapshot(*slots: BackendSlot) -> dict[str, BackendSlot]:
    return {slot["backend"].id: slot for slot in slots}


def _cfg(*, spill_after: int = 900, max_attempts: int = 3) -> ControlSettings:
    return ControlSettings(
        cloud_spill_to_local_after_seconds=spill_after,
        cloud_submit_max_attempts=max_attempts,
    )


# --- SCHED-01: rank-first eligible dispatch ----------------------------------------------


def test_rank_first_picks_lowest_rank_available() -> None:
    """Two available backends (rank 0 and rank 5, both with a free slot) -> the rank-0 one."""
    b0 = _compute(id="rank0", rank=0, cap=4)
    b5 = _compute(id="rank5", rank=5, cap=4)
    snap = _snapshot(_slot(b0, remaining=4), _slot(b5, remaining=4))
    picked = select_backend(NOW, 0, snap, NOW, _cfg())
    assert picked is b0


def test_spill_when_lowest_rank_full_picks_next_rank() -> None:
    """rank-0 backend is full (remaining==0), rank-5 has a slot -> spill to rank-5 (SCHED-01)."""
    b0 = _compute(id="rank0", rank=0, cap=4)
    b5 = _compute(id="rank5", rank=5, cap=4)
    snap = _snapshot(_slot(b0, remaining=0), _slot(b5, remaining=4))
    picked = select_backend(NOW, 0, snap, NOW, _cfg())
    assert picked is b5


# --- MCOMP-04: N compute lanes filled lowest-rank-first, then spill to the next rank ------


def test_mcomp04_compute_rank_cap_spread_prefers_free_arm64_then_spills_to_paid_x86() -> None:
    """MCOMP-04 (tiered spread): N compute backends fill lowest-rank-first up to cap, then spill to the next rank.

    Two compute lanes -- a cheaper free-arm64 lane (rank 10) preferred over a paid-x86 lane (rank 20).
    While the free lane has a free slot it always wins (rank-first); once the free lane is full
    (``remaining == 0``, i.e. its per-agent cap is spent this tick) the drain spills the next FIFO
    candidate to the paid lane rather than holding. This is the per-agent-cap load-spread across N
    compute backends the tiered drain performs (extends ``test_spill_when_lowest_rank_full_picks_next_rank``
    to explicitly-labelled compute lanes).
    """
    free_arm64 = _compute(id="free-arm64", rank=10, cap=2)
    paid_x86 = _compute(id="paid-x86", rank=20, cap=2)

    # Both lanes have free slots -> the lower-rank (free) lane wins.
    both_free = _snapshot(_slot(free_arm64, remaining=2), _slot(paid_x86, remaining=2))
    assert select_backend(NOW, 0, both_free, NOW, _cfg()) is free_arm64

    # Free lane at cap (remaining==0) -> spill the next candidate to the paid lane, not a hold.
    free_full = _snapshot(_slot(free_arm64, remaining=0), _slot(paid_x86, remaining=2))
    assert select_backend(NOW, 0, free_full, NOW, _cfg()) is paid_x86


# --- D-03: offline -> local is immediate (NOT staleness-gated) ----------------------------


def test_offline_all_non_local_spills_to_local_immediately() -> None:
    """Every non-local backend OFFLINE + local available + file just entered -> local, no wait."""
    compute = _compute(id="compute-a1", rank=10, cap=2)
    local = _local()
    snap = _snapshot(
        _slot(compute, available=False, remaining=2),
        _slot(local, remaining=10),
    )
    # updated_at == now -> zero wait; offline path must NOT be gated by the staleness threshold.
    picked = select_backend(NOW, 0, snap, NOW, _cfg())
    assert picked is local


# --- D-01: online-but-FULL -> local is staleness-gated ------------------------------------


def test_stale_full_to_local_gated_before_threshold_holds() -> None:
    """Non-local online-but-FULL + file waited < threshold -> local NOT eligible -> hold (None)."""
    compute = _compute(id="compute-a1", rank=10, cap=2)
    local = _local()
    snap = _snapshot(
        _slot(compute, available=True, remaining=0),  # online but full
        _slot(local, remaining=10),
    )
    entered = NOW - timedelta(seconds=300)  # lane entry 300s ago < 900s threshold
    assert select_backend(entered, 0, snap, NOW, _cfg(spill_after=900)) is None


def test_stale_full_to_local_after_threshold_spills() -> None:
    """Same online-but-FULL cloud, but the file waited >= threshold -> local becomes eligible."""
    compute = _compute(id="compute-a1", rank=10, cap=2)
    local = _local()
    snap = _snapshot(
        _slot(compute, available=True, remaining=0),
        _slot(local, remaining=10),
    )
    entered = NOW - timedelta(seconds=1200)  # lane entry 1200s ago >= 900s threshold
    assert select_backend(entered, 0, snap, NOW, _cfg(spill_after=900)) is local


# --- D-04: attempt-exclusion forces local -------------------------------------------------


def test_attempt_exhausted_excludes_cloud_routes_local() -> None:
    """cloud_attempts >= cfg.cloud_submit_max_attempts -> only local eligible, even with cloud slots free."""
    compute = _compute(id="compute-a1", rank=10, cap=2)
    local = _local()
    snap = _snapshot(
        _slot(compute, available=True, remaining=2),  # cloud has free slots
        _slot(local, remaining=10),
    )
    # attempts == max (3) -> cloud excluded; local not staleness-gated because budget spent.
    picked = select_backend(NOW, 3, snap, NOW, _cfg(max_attempts=3))
    assert picked is local


def test_attempt_below_max_still_prefers_cloud() -> None:
    """cloud_attempts < max leaves cloud eligible -> the lower-rank cloud backend still wins."""
    compute = _compute(id="compute-a1", rank=10, cap=2)
    local = _local()
    snap = _snapshot(_slot(compute, remaining=2), _slot(local, remaining=10))
    picked = select_backend(NOW, 2, snap, NOW, _cfg(max_attempts=3))
    assert picked is compute


# --- D-06: stateless re-rank --------------------------------------------------------------


def test_stateless_rerank_repicks_same_backend_after_prior_failure() -> None:
    """No last-failed memory: identical inputs re-pick the same lowest-rank backend across ticks."""
    compute = _compute(id="compute-a1", rank=10, cap=2)
    local = _local()
    snap = _snapshot(_slot(compute, remaining=2), _slot(local, remaining=10))
    # Simulate a prior tick that "failed" on compute; the counter has not yet crossed the cap.
    first = select_backend(NOW, 1, snap, NOW, _cfg(max_attempts=3))
    second = select_backend(NOW, 1, snap, NOW, _cfg(max_attempts=3))
    assert first is compute
    assert second is compute  # re-picked, no per-file backend memory


def test_stateless_signature_has_no_last_failed_parameter() -> None:
    """D-06: the function carries no 'last-failed backend' / history parameter."""
    params = list(inspect.signature(select_backend).parameters)
    assert params == ["lane_entered_at", "cloud_attempts", "snapshot", "now", "cfg"]


# --- SCHED-04: tie-break by utilization then stable id ------------------------------------


def test_tiebreak_prefers_lower_utilization() -> None:
    """Two available equal-rank backends -> the one at lower in_flight/cap utilization wins."""
    busy = _compute(id="compute-busy", rank=10, cap=4)
    idle = _compute(id="compute-idle", rank=10, cap=4)
    snap = _snapshot(
        _slot(busy, remaining=1, cap=4),  # util = (4-1)/4 = 0.75
        _slot(idle, remaining=3, cap=4),  # util = (4-3)/4 = 0.25
    )
    picked = select_backend(NOW, 0, snap, NOW, _cfg())
    assert picked is idle


def test_tiebreak_equal_utilization_breaks_on_stable_id() -> None:
    """Equal rank AND equal utilization -> the lexicographically-smaller id wins (stable)."""
    a = _compute(id="compute-a", rank=10, cap=4)
    b = _compute(id="compute-b", rank=10, cap=4)
    snap = _snapshot(
        _slot(a, remaining=2, cap=4),  # util 0.5
        _slot(b, remaining=2, cap=4),  # util 0.5
    )
    picked = select_backend(NOW, 0, snap, NOW, _cfg())
    assert picked is a


# --- hold: no eligible backend ------------------------------------------------------------


def test_hold_returns_none_when_no_backend_eligible() -> None:
    """No available backend with a free slot -> None (clean hold), never raises."""
    compute = _compute(id="compute-a1", rank=10, cap=2)
    kueue = _kueue(id="kueue-x64", rank=20, cap=5)
    snap = _snapshot(
        _slot(compute, available=False, remaining=0),
        _slot(kueue, available=True, remaining=0),
    )
    # No local slot at all + all cloud unavailable/full + fresh file -> hold.
    assert select_backend(NOW, 0, snap, NOW, _cfg()) is None


def test_hold_when_empty_snapshot() -> None:
    """An empty snapshot -> None (hold), never raises / never divides by zero."""
    assert select_backend(NOW, 0, {}, NOW, _cfg()) is None
