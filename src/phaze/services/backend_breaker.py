"""The per-backend control-plane-unreachable circuit breaker (phaze-j0ixx).

WHY IT EXISTS. On 2026-09-26/27 two infrastructure faults -- the app host's outage, then the burst
node's cluster DNS losing its upstream -- made every burst pod fail its presign request. The pod exited
``EXIT_DOWNLOAD`` (10), which reconcile charged against the file's cloud ``attempts`` exactly like a
failed object GET, so each file burned one submit plus three re-drives in about three minutes and
spilled: 302 healthy files lost their whole cloud budget to two blips (spike ``phaze-79mu7``).

Operator decision 2026-09-27 (``AskUserQuestion``, dispatch session). Question as put: *"phaze-j0ixx:
when cloud pods fail because the control plane is unreachable (the 09-26 outage and the vox DNS loss),
files currently use up their 3 attempts. How should phaze respond instead?"* Answer as given (selected
option label): *"Both: exit code + breaker (Recommended)"*. Durable record: the operator-decision
comment on bead ``phaze-j0ixx``. The thresholds and the probe design below were left to the
implementer and are the IMPLEMENTER'S decision, not the operator's.

THE TWO HALVES.

1. **Exit code.** ``job_runner`` exits ``EXIT_CONTROL_PLANE_UNREACHABLE`` (14) when its presign request
   could not open a connection to the control plane at all (DNS failure, connection refused, connect
   timeout). Reconcile never charges ``attempts`` (or ``node_loss_redrives``) for it.
2. **Breaker.** Every such exit is fed here. When the backend trips, the drain stops giving it slots,
   reconcile spills its unreachable rows back to ``'awaiting'`` uncharged instead of re-driving them,
   and the Analyze page's lane card shows the hold.

THE TRIP RULE (implementer's decision). A backend trips when EITHER holds, counting only exits after
the breaker's last close (``reset_at``):

* **3 distinct files** (:data:`TRIP_DISTINCT_FILES`) exited 14 on it within **15 minutes**
  (:data:`TRIP_WINDOW_SECONDS`). Three is below the 4 slots the burst lane refilled per drain tick in
  the incident (spike ``phaze-79mu7``), so a lane whose whole in-flight set fails at once trips on the
  first reconcile tick that sees it, and "distinct" keeps one
  file from tripping the lane for everyone. Fifteen minutes is three drain ticks: long enough that a
  lane whose pods fail one or two at a time still accumulates, short enough that two unrelated blips an
  hour apart do not add up.
* **the same file exited 14 twice** within that window. The exit is file-independent by construction
  (whether a TCP connection to the control plane opens does not depend on which file the pod was given),
  so a second one on the same row is the environment again. Without this clause a lone in-flight file
  would re-drive, uncharged, once a minute forever -- "not charged" silently becoming "not bounded",
  exactly the trap ``phaze-1q4g`` closed for node loss. With it, one file costs at most two pods before
  the backend is held.

The count is DERIVED from ``cloud_job`` (``last_exit_code`` / ``last_failed_at``, phaze-1xngw) rather
than kept as a tally here: ``cloud_job`` is one row per file, so "distinct files" is a plain count, and
re-feeding the same exit (the still-terminating deferral re-enters on a later tick) cannot double-count.

HOLD, NOT OFFLINE. A held backend keeps ``available=True`` in the drain snapshot and gets ``remaining=0``.
``select_backend`` spills a file to local IMMEDIATELY when every cloud backend is offline, but only
after ``cloud_spill_to_local_after_seconds`` when cloud is merely full -- so holding the lane as "full"
leaves the local lane's behaviour exactly what it is on any busy day instead of flooding it with the
whole awaiting queue the moment the breaker opens.

THE PROBE (implementer's decision). While open, the drain grants the held backend ONE slot once
``next_probe_at`` has passed and pushes ``next_probe_at`` 15 minutes on (:data:`PROBE_INTERVAL_SECONDS`),
so at most one probe pod per 15 minutes. The breaker CLOSES on positive proof: the API's presign endpoint
closes it the moment it mints a URL for a file on that backend -- a pod on that backend has just reached
the control plane, which is precisely the property that was broken. Nothing closes it on a timer, and a
probe that exits 14 again simply leaves it open with the next probe 15 minutes out. Measured against
the incident: 7 h of DNS loss costs at most ~28 probe pods and zero charged attempts, against the 298
files it poisoned.

Nothing here kills a running pod (D-08 / phaze-202e): a held backend's in-flight analyses run to
completion; the breaker only gates new dispatch.

Every function NEVER commits -- the caller owns the transaction (the drain's single post-loop commit,
reconcile's per-row commit, the presign handler's own commit).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.models.backend_breaker import BackendBreaker
from phaze.models.cloud_job import CloudJob


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

# ``job_runner.EXIT_CONTROL_PLANE_UNREACHABLE``, restated rather than imported so the control plane does
# not load the pod's entrypoint module; ``tests/analyze/recovery_cloud/redrive/test_control_plane_breaker.py``
# pins the two equal.
UNREACHABLE_EXIT_CODE = 14

TRIP_DISTINCT_FILES = 3
TRIP_WINDOW_SECONDS = 15 * 60
PROBE_INTERVAL_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class OpenBreaker:
    """An open breaker as the drain and the Analyze page read it."""

    backend_id: str
    tripped_at: datetime
    trip_reason: str | None
    next_probe_at: datetime | None


class UnreachableVerdict(NamedTuple):
    """What :func:`record_unreachable_exit` decided: whether the backend is now held, and why."""

    held: bool
    tripped_now: bool
    reason: str | None


def _aware(value: datetime | None) -> datetime | None:
    """Assume UTC for a naive timestamp (the scan_reaper convention) so comparisons never raise."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _lock_breaker_row(session: AsyncSession, backend_id: str) -> BackendBreaker:
    """Create the backend's row if absent, then return it row-locked (serializes against the presign close)."""
    await session.execute(pg_insert(BackendBreaker).values(backend_id=backend_id).on_conflict_do_nothing(index_elements=["backend_id"]))
    stmt = select(BackendBreaker).where(BackendBreaker.backend_id == backend_id).with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one()


async def record_unreachable_exit(
    session: AsyncSession,
    backend_id: str,
    file_id: uuid.UUID,
    now: datetime,
    *,
    previous_exit_code: int | None,
    previous_failed_at: datetime | None,
) -> UnreachableVerdict:
    """Feed one ``EXIT_CONTROL_PLANE_UNREACHABLE`` terminal into ``backend_id``'s breaker; trip it if the rule holds.

    Call BEFORE the row's own ``last_exit_code`` / ``last_failed_at`` are overwritten with this exit:
    ``previous_*`` are that row's prior recorded failure (the repeat clause), and the distinct-file count
    excludes ``file_id`` and adds it back as one, so the answer never depends on an autoflush of the
    caller's pending writes. An already-open breaker stays open and pushes its next probe out, because
    this exit is fresh evidence the fault persists. NEVER commits.
    """
    breaker = await _lock_breaker_row(session, backend_id)
    if breaker.tripped_at is not None:
        breaker.next_probe_at = now + timedelta(seconds=PROBE_INTERVAL_SECONDS)
        return UnreachableVerdict(held=True, tripped_now=False, reason=breaker.trip_reason)

    since = now - timedelta(seconds=TRIP_WINDOW_SECONDS)
    reset_at = _aware(breaker.reset_at)
    if reset_at is not None and reset_at > since:
        since = reset_at
    previous = _aware(previous_failed_at)
    repeat = previous_exit_code == UNREACHABLE_EXIT_CODE and previous is not None and previous > since
    others = (
        await session.execute(
            select(func.count())
            .select_from(CloudJob)
            .where(
                CloudJob.backend_id == backend_id,
                CloudJob.last_exit_code == UNREACHABLE_EXIT_CODE,
                CloudJob.last_failed_at > since,
                CloudJob.file_id != file_id,
            )
        )
    ).scalar_one()
    distinct_files = int(others) + 1
    if repeat:
        reason = f"file {file_id} exited {UNREACHABLE_EXIT_CODE} (control plane unreachable) twice within {TRIP_WINDOW_SECONDS // 60} min"
    elif distinct_files >= TRIP_DISTINCT_FILES:
        reason = f"{distinct_files} files exited {UNREACHABLE_EXIT_CODE} (control plane unreachable) within {TRIP_WINDOW_SECONDS // 60} min"
    else:
        return UnreachableVerdict(held=False, tripped_now=False, reason=None)
    breaker.tripped_at = now
    breaker.trip_reason = reason
    breaker.next_probe_at = now + timedelta(seconds=PROBE_INTERVAL_SECONDS)
    logger.warning(
        "backend breaker OPEN: pods on this backend cannot reach the control plane -- dispatch held, attempts not charged",
        backend_id=backend_id,
        reason=reason,
        next_probe_at=breaker.next_probe_at.isoformat(),
    )
    return UnreachableVerdict(held=True, tripped_now=True, reason=reason)


async def load_open_breakers(session: AsyncSession) -> dict[str, OpenBreaker]:
    """Return every open breaker keyed by backend id; ``{}`` (all closed) on ANY read failure. Never raises.

    Runs inside a SAVEPOINT (the ``route_control`` pattern) because both callers -- the drain tick and
    the 5 s Analyze poll -- hold a session whose transaction must survive a hiccup here. Failing OPEN
    (treat as closed) is deliberate: the breaker only saves pods and re-uploads, the uncharged exit path
    protects the budgets on its own, and a breaker read that failed CLOSED would silently stop the lane.
    """
    try:
        async with session.begin_nested():
            rows = (await session.execute(select(BackendBreaker).where(BackendBreaker.tripped_at.is_not(None)))).scalars().all()
    except Exception:
        logger.warning("backend_breaker_read_degraded", exc_info=True)
        return {}
    return {
        row.backend_id: OpenBreaker(
            backend_id=row.backend_id,
            tripped_at=cast("datetime", _aware(row.tripped_at)),
            trip_reason=row.trip_reason,
            next_probe_at=_aware(row.next_probe_at),
        )
        for row in rows
    }


def gate_remaining(breaker: OpenBreaker | None, remaining: int, now: datetime) -> tuple[int, bool]:
    """Return ``(slots the drain may use on this backend, whether one of them is a probe)``. Pure.

    Closed -> ``remaining`` untouched. Open and not yet due -> 0. Open and due -> at most ONE slot, the
    probe; ``(0, False)`` when the backend has no free slot to probe with.
    """
    if breaker is None:
        return remaining, False
    if breaker.next_probe_at is not None and now < breaker.next_probe_at:
        return 0, False
    probe_slots = min(remaining, 1)
    return probe_slots, probe_slots > 0


async def arm_next_probe(session: AsyncSession, backend_id: str, now: datetime) -> None:
    """Push an open breaker's ``next_probe_at`` one interval on, after the drain granted a probe slot. NEVER commits."""
    await session.execute(
        update(BackendBreaker)
        .where(BackendBreaker.backend_id == backend_id, BackendBreaker.tripped_at.is_not(None))
        .values(next_probe_at=now + timedelta(seconds=PROBE_INTERVAL_SECONDS))
    )


async def close_breaker(session: AsyncSession, backend_id: str, now: datetime, *, evidence: str) -> bool:
    """Close ``backend_id``'s breaker if it is open; return whether it was. NEVER commits.

    ``reset_at = now`` retires every unreachable exit recorded before this proof of reachability, so the
    failures that opened the breaker cannot immediately re-trip it.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(BackendBreaker)
            .where(BackendBreaker.backend_id == backend_id, BackendBreaker.tripped_at.is_not(None))
            .values(tripped_at=None, trip_reason=None, next_probe_at=None, reset_at=now)
        ),
    )
    closed = result.rowcount > 0
    if closed:
        logger.warning(
            "backend breaker CLOSED: a pod on this backend reached the control plane -- dispatch resumes", backend_id=backend_id, evidence=evidence
        )
    return closed
