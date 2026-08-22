"""Phase 71 (BEUI-01): the read-only backend-lane snapshot behind the 5s ``/pipeline/stats`` poll.

Extracted verbatim from the former single-module ``services/backends.py`` (phaze-dr9df).

A pure read over the Phase-67 registry + the ``cloud_job`` in-flight/admission substrate that feeds
the BEUI-01 N-lane grid. Every leg is degrade-safe -- a DB hiccup or a hung Kueue probe can NEVER
raise into the hot poll (T-71-03) -- and secret-free: only
``{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}`` (plus the phaze-5c6i2
queued/working/processed additions from :mod:`~phaze.services.backends.lane_metrics`) ever leaves
this module; a probe-failure log carries ``backend_id`` ONLY, never a SecretStr / kube SA token / S3
key (SP-5, T-71-01).

Two derived flags live here too, deliberately next to the snapshot they read so they can never drift
from it: :func:`derive_localqueue_unreachable` (the K8s LocalQueue amber alert) and
:func:`derive_cloud_hold_reason` (the Cloud Routing card's sub-caption, which mirrors the drain's own
gate ORDER).

This is the TOP of the package DAG -- it depends on the registry, on every backend impl (for the
``isinstance`` lane-kind derivation) and on both lane-data modules, and nothing in the package
depends on it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.services.backends.compute_agent import ComputeAgentBackend
from phaze.services.backends.kueue import KueueBackend
from phaze.services.backends.lane_metrics import (
    _cloud_lane_active,
    _cloud_lane_queued_working,
    _lane_processed_counts,
    _local_lane_queued_working,
    _safe_count_or_none,
)
from phaze.services.backends.local import LocalBackend
from phaze.services.backends.registry import resolve_backends
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.route_control import get_route_control


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.services.backends.base import Backend


logger = structlog.get_logger(__name__)


# D-02/A2: the per-probe availability timeout -- well under the 5s poll, yet tolerant of a slow-healthy
# kr8s ``LocalQueue`` RTT. A hung Kueue cluster times out to "offline" for THAT lane alone (T-71-02).
_PROBE_TIMEOUT_SEC = 1.5

# The zero-admission fallback merged into a lane with no attributed ``cloud_job`` rows (idle/local lanes).
_ZERO_ADMISSION: dict[str, int] = {"quota_wait": 0, "inadmissible": 0}


async def _rollback_and_log(session: AsyncSession, event: str, **log_kwargs: Any) -> None:
    """Roll back ``session``, logging (never raising) if the rollback itself fails (bead phaze-bk9el.2).

    Shared by every degrade-safe except-branch in this module: a cleanup failure must never mask the
    real failure the caller is already reporting, so this always returns normally regardless of what
    ``session.rollback()`` raises. Deliberately broad (``except Exception``, not narrowed to
    ``SQLAlchemyError``): ``test_admission_degrades_when_rollback_also_fails``,
    ``test_probe_one_swallows_a_rollback_failure_after_a_failed_probe`` and
    ``test_snapshot_degrades_when_rollback_also_fails`` (``tests/shared/services/test_lane_snapshot.py``)
    all pin a GENERIC (non-SQLAlchemy) rollback failure being swallowed here, not just a DB-layer one --
    narrowing the except type would break those tests and, more importantly, would reopen a path for a
    cleanup-time bug to propagate into the hot 5s ``/pipeline/stats`` poll (T-71-03), which every caller
    of this helper exists specifically to prevent.
    """
    try:
        await session.rollback()
    except Exception:
        logger.warning(event, exc_info=True, **log_kwargs)


async def _admission_by_backend_id(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Return per-``backend_id`` admission counts ``{quota_wait, inadmissible}`` via one ``GROUP BY`` (D-03).

    Generalizes the GLOBAL ``pipeline.get_cloud_phase_counts`` (``cloud_phase == QUEUED_BEHIND_QUOTA``) +
    ``pipeline.get_inadmissible_count`` (``inadmissible`` AND ``status IN {SUBMITTED, RUNNING}``) predicates
    to a per-``backend_id`` ``GROUP BY`` so each Kueue lane owns its OWN quota-wait-vs-Inadmissible counts.
    ``cloud_phase`` is NULL for local/compute rows, so they contribute 0 to ``quota_wait``; ``backend_id``
    -NULL (legacy / unattributed) rows are excluded entirely (they belong to no lane). Degrades to ``{}``
    on any DB error with a guarded rollback (mirrors ``pipeline._safe_count``) so it never raises into the
    hot 5s poll (T-71-03).
    """
    try:
        stmt = (
            select(
                CloudJob.backend_id,
                func.count().filter(CloudJob.cloud_phase == CloudPhase.QUEUED_BEHIND_QUOTA.value).label("quota_wait"),
                func.count()
                .filter(
                    CloudJob.inadmissible.is_(True),
                    CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
                )
                .label("inadmissible"),
            )
            .where(CloudJob.backend_id.is_not(None))
            .group_by(CloudJob.backend_id)
        )
        rows = (await session.execute(stmt)).all()
    except Exception:
        # Broad by design (T-71-03): test_admission_degrades_to_empty_on_db_error pins a GENERIC
        # exception -- not just a SQLAlchemy one -- degrading this to {} rather than raising into the
        # hot poll, so this stays an unnarrowed catch-all.
        logger.warning("backend_lane_admission_degraded", exc_info=True)
        await _rollback_and_log(session, "backend_lane_admission_rollback_failed")
        return {}
    return {backend_id: {"quota_wait": int(quota_wait or 0), "inadmissible": int(inadmissible or 0)} for backend_id, quota_wait, inadmissible in rows}


async def _probe_one(session: AsyncSession, backend: Backend) -> tuple[str, bool]:
    """Probe ONE backend's live availability, bounded + degrade-safe -> ``(backend_id, available)`` (D-02).

    A :class:`LocalBackend` is short-circuited to ``True`` with NO I/O (local dispatch never depends on a
    remote agent). Every other backend's ``is_available`` is awaited under an ``asyncio.wait_for`` bounded
    by ``_PROBE_TIMEOUT_SEC``; a timeout OR any probe exception degrades THAT lane to offline and logs the
    ``backend_id`` ONLY (never a SecretStr / kube token, T-71-01). A single hung Kueue cluster can
    therefore never stall the shared read (T-71-02).

    phaze-ntr8s: a compute probe's DB read (``select_agent_by_id`` -> ``session.execute``) cancelled
    mid-flight by the ``asyncio.wait_for`` timeout can leave the SHARED session unusable for the next
    statement -- SQLAlchemy raises ``PendingRollbackError`` on the following ``execute`` until the
    session is rolled back. ``_probe_availability`` runs every backend's probe SEQUENTIALLY on this ONE
    session (Pitfall 1), so without an immediate roll back HERE that poison outlives this probe: the very
    next backend's probe (or, for a Kueue probe that ignores the session, whichever LATER probe next
    touches it) inherits a broken session and fails immediately -- a single slow compute lane cascading
    every SUBSEQUENT lane in the SAME sweep to a false "offline", which then reports a false "no cloud
    backend reachable" hold reason for a transient DB blip that has nothing to do with reachability. Roll
    back HERE, inside the per-probe except, not just once after the whole fan-out (the pre-existing
    post-fan-out rollback in :func:`get_backend_lane_snapshot` only protected the NEXT poll's snapshot,
    never a sibling lane within THIS one). ``session.rollback()`` is a safe no-op when there is nothing to
    roll back, and any failure rolling back is itself swallowed -- a cleanup failure must never mask the
    real probe failure this branch is already reporting.
    """
    if isinstance(backend, LocalBackend):
        return (backend.id, True)
    try:
        available = await asyncio.wait_for(backend.is_available(session), _PROBE_TIMEOUT_SEC)
    except Exception:
        # Broad by design: `backend.is_available` fans out to heterogeneous impls (kube client,
        # compute-agent DB lookup, ...) that can raise almost anything, plus asyncio.wait_for's own
        # TimeoutError -- see the docstring above for the full session-poisoning rationale (phaze-ntr8s).
        logger.info("backend_lane_probe_offline", backend_id=backend.id)
        await _rollback_and_log(session, "backend_lane_probe_rollback_failed", backend_id=backend.id)
        return (backend.id, False)
    return (backend.id, bool(available))


async def _probe_availability(session: AsyncSession, backends: list[Backend]) -> dict[str, bool]:
    """Probe every backend SEQUENTIALLY on the one shared session -> ``{backend_id: available}`` (D-02).

    The probes run one at a time in a plain ``for`` loop: each ``_probe_one`` is fully awaited before the
    next begins, so there is NEVER concurrent use of the shared ``AsyncSession`` -- Session-safety
    (Pitfall 1) holds by CONSTRUCTION, guaranteed by the serial control flow. Since Phase 72 (MCOMP-01) retired the
    single-active-compute assumption, N≥2 compute backends are legal and each compute probe touches the
    shared ``session`` via ``select_agent_by_id`` (``session.execute``); serializing the fan-out guarantees
    those ``session.execute`` calls can never overlap (SQLAlchemy forbids concurrent operations on one
    session). Each ``_probe_one`` is individually capped by ``asyncio.wait_for(..., _PROBE_TIMEOUT_SEC)``
    AND rolls the session back itself on a timeout/exception (phaze-ntr8s), so one poisoned probe can
    never cascade to the NEXT backend probed in this same loop. Because the probes now run one at a time
    the worst-case aggregate wait is ``N x _PROBE_TIMEOUT_SEC`` (not the old ``asyncio.gather`` ~1x
    bound) -- a deliberate D-01 trade-off, acceptable because N is small (registry-declared local +
    N-Kueue + N-compute) and session-safety takes priority over probe latency on the 5s
    ``/pipeline/stats`` poll. The post-fan-out ``session.rollback`` in :func:`get_backend_lane_snapshot`
    is retained as a second, harmless line of defense before the ``in_flight_count`` reads. Kueue probes
    ignore the session (kr8s I/O) and local is short-circuited (no I/O).
    """
    results: dict[str, bool] = {}
    for backend in backends:
        backend_id, available = await _probe_one(session, backend)
        results[backend_id] = available
    return results


def _kind_of(backend: Backend) -> str:
    """Derive the lane ``kind`` ("local"/"compute"/"kueue") from the impl class (mirrors resolve_backends)."""
    if isinstance(backend, LocalBackend):
        return "local"
    if isinstance(backend, ComputeAgentBackend):
        return "compute"
    if isinstance(backend, KueueBackend):
        return "kueue"
    return "unknown"


async def get_backend_lane_snapshot(session: AsyncSession, app_state: Any = None) -> list[dict[str, Any]]:
    """Return one rank-ascending, secret-free lane dict per registry backend for the BEUI-01 grid.

    Resolves the Phase-67 registry, then composes one lane per backend from several degrade-safe reads:
    ``_admission_by_backend_id`` (per-``backend_id`` quota_wait/inadmissible, D-03), ``_probe_availability``
    (live bounded is_available probes, D-02), each backend's ``in_flight_count`` (the D-02 cloud_job
    substrate) and, since phaze-5c6i2, the queued/working/processed metrics below. Lanes are sorted
    rank-ascending, tie-broken by ``id`` (D-06), so the Plan-03 template loops them verbatim. A
    :class:`LocalBackend` lane always shows ``in_flight`` 0 and ``available`` True.

    ``app_state`` (phaze-5c6i2) is threaded through ONLY to resolve the local lane's SAQ queue via
    ``app_state.task_router`` (:func:`_local_lane_queued_working`) -- every render caller already has
    ``request.app.state`` at hand (the same object :func:`get_lane_queue_depths` takes). It defaults to
    ``None`` for callers that only need availability/admission (e.g. :func:`derive_cloud_hold_reason`,
    and every pre-phaze-5c6i2 test): the local lane's ``queued``/``working`` degrade to ``None``
    (explicit unknown) rather than requiring every caller to thread a router it does not otherwise need.

    Every lane carries ``{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible, queued,
    working, active, processed_24h, processed_lifetime}`` -- no ``config``, no ``SecretStr``, no kube/S3 token
    (T-71-01). ``in_flight`` is UNCHANGED (still the D-02 cloud_job substrate; several other callers key
    off it, e.g. :func:`derive_localqueue_unreachable` / :func:`derive_cloud_hold_reason` /
    ``_lane_detail.html``'s header numeral). ``queued``/``working``/``processed_24h``/
    ``processed_lifetime`` are the phaze-5c6i2 additions the lane cards render INSTEAD of the misleading
    ``{in_flight}/{cap}`` numeral + saturation bar (acceptance rule 1): ``queued``/``working`` come from
    :func:`_local_lane_queued_working` (local) or :func:`_cloud_lane_queued_working` (compute/kueue, the
    phaze-zyoag seam), ``processed_24h``/``processed_lifetime`` from :func:`_lane_processed_counts`. Each
    of the four is ``int | None`` -- ``None`` means degraded/unknown (never a fabricated 0, acceptance
    rule 8) and the template renders an em-dash for it. Any top-level exception degrades the WHOLE
    snapshot to ``[]`` with a guarded rollback so it can NEVER raise into the hot 5s ``/pipeline/stats``
    poll (SP-1, T-71-03) -- unchanged from before this bead.
    """
    try:
        backends = resolve_backends(cast("ControlSettings", get_settings()))
        admission = await _admission_by_backend_id(session)
        availability = await _probe_availability(session, backends)
        # T-71-02 per-lane isolation: a compute ``is_available`` probe can fail at the DB layer
        # (not just time out), poisoning the shared session. Clear it after the fan-out -- the
        # snapshot does no writes, so a rollback here is safe -- so one bad lane degrades to
        # ``available=False`` (via ``_probe_one``) instead of poisoning the subsequent
        # ``in_flight_count`` reads and collapsing the WHOLE grid to the ``[]`` degrade panel.
        await session.rollback()
        lanes: list[dict[str, Any]] = []
        for backend in backends:
            kind = _kind_of(backend)
            if kind == "local":
                queued, working = await _local_lane_queued_working(session, app_state)
                active = working
                processed_24h, processed_lifetime = await _lane_processed_counts(session, backend_id=None)
            else:
                queued, working = await _cloud_lane_queued_working(session, backend.id)
                active = await _cloud_lane_active(session, backend.id, kind)
                processed_24h, processed_lifetime = await _lane_processed_counts(session, backend_id=backend.id)
            lanes.append(
                {
                    "id": backend.id,
                    "kind": kind,
                    "rank": backend.rank,
                    "cap": backend.cap,
                    "in_flight": await backend.in_flight_count(session),
                    "available": availability.get(backend.id, False),
                    "queued": queued,
                    "working": working,
                    "active": active,
                    "processed_24h": processed_24h,
                    "processed_lifetime": processed_lifetime,
                    **admission.get(backend.id, _ZERO_ADMISSION),
                }
            )
        lanes.sort(key=lambda lane: (lane["rank"], lane["id"]))
    except Exception:
        # Broad by design (T-71-03, SP-1) -- see the docstring above: any top-level exception, from any
        # of the several DB reads and pluggable-backend calls above, degrades the WHOLE snapshot to [].
        logger.warning("backend_lane_snapshot_degraded", exc_info=True)
        await _rollback_and_log(session, "backend_lane_snapshot_rollback_failed")
        return []
    return lanes


def derive_localqueue_unreachable(lanes: list[dict[str, Any]]) -> bool:
    """Return True iff ANY kueue lane in ``lanes`` is unreachable -- the K8s LocalQueue amber alert (D-05).

    Phase 56/70 (KDEPLOY-04, MKUE-01/03) originally drove this from a cross-process Redis flag the
    controller's startup probe wrote once at boot (D-05/D-06). phaze-6r39 retired that mechanism: the
    flag was a boot-time SNAPSHOT with no TTL and no other writer, so it (a) never cleared once
    connectivity was restored (the reported bug) and (b) never appeared at all for an outage that began
    AFTER boot -- the alert was structurally incapable of firing for the exact class of event it exists
    to surface. Every 5s ``/pipeline/stats`` poll already probes the SAME LocalQueue live via
    :func:`get_backend_lane_snapshot` -> ``_probe_availability`` -> ``KueueBackend.is_available``, so
    this derives the flag from that snapshot instead of a second, staler read.

    Preserves the original aggregate semantic: unreachable iff ANY configured kueue backend is
    unreachable (reachable == ALL-reachable). Zero kueue lanes (all-local / compute-only, the WR-01
    case) makes ``any(...)`` False for free -- no special-casing needed, and no Redis key is read or
    written on this path at all any more. ``lanes`` is already degrade-safe (``get_backend_lane_snapshot``
    -> ``[]`` on any error), so a fully degraded snapshot silently reports "reachable" rather than
    surfacing a false alarm -- the same fail-silent posture the retired Redis-flag reader
    (``get_localqueue_unreachable``, removed by this change) used to provide on a Redis hiccup.
    """
    return any(lane["kind"] == "kueue" and not lane["available"] for lane in lanes)


# The neutral, no-causal-claim copy any unexpected error in derive_cloud_hold_reason degrades to --
# the card must never assert a specific blocker it has not actually confirmed.
_HOLD_REASON_DEGRADED = "held"


async def derive_cloud_hold_reason(session: AsyncSession) -> str:
    """Return the Cloud Routing card's truthful sub-caption, mirroring the drain's own gate order.

    The routing-only snapshot uses the SAME backend implementations and ``{available, cap,
    in_flight}`` inputs as :func:`get_backend_lane_snapshot`, without paying for unrelated lane-card
    metrics. This checks the SAME gates ``release_awaiting_cloud.stage_cloud_window`` checks, in order:
    cloud disabled -> :func:`get_route_control` force-local -> no lane reachable -> every reachable
    lane full -> no fileserver agent online -> else genuinely queued with free capacity. A prior
    incarnation of this card hardcoded "no compute agent online" regardless of the real blocker
    (T-83-hold-reason-bug); this derivation can only ever name a gate it has actually observed.

    Every read here is individually degrade-safe (``get_route_control`` / the routing snapshot
    never raise), but the surrounding ``try/except`` is the belt: ANY unexpected exception -- including
    one from ``get_settings()`` or the fileserver probe -- collapses to the neutral
    :data:`_HOLD_REASON_DEGRADED` copy with NO causal claim, so the hot 5s poll can never 500 on a
    hiccup here (mirrors the T-71-03 idiom this module already applies throughout).
    """
    try:
        cfg = cast("ControlSettings", get_settings())
        if not cfg.cloud_enabled:
            return "cloud routing disabled"
        if await get_route_control(session):
            return "held — cloud routing paused (force-local)"

        lanes = await _get_backend_routing_snapshot(session, cfg)
        if lanes is None:
            return _HOLD_REASON_DEGRADED
        # phaze-g4fh: restrict reachability/capacity math to CLOUD lanes. A local lane is always
        # `available=True` with `in_flight=0` (LocalBackend.is_available/in_flight_count), so
        # including it here made `available_lanes` never empty and `free_slots` always ≥1 -- both
        # the "no cloud backend reachable" and "all lanes at capacity" branches below were dead code
        # even when every real cloud lane was online-but-full. select_backend gates local behind
        # `cloud_spill_to_local_after_seconds` staleness (D-01/D-03), so local is a delayed safety
        # net the drain would NOT dispatch to next tick -- it is not free cloud capacity, and this
        # derivation must mirror exactly what the drain would do next tick (T-83 anti-goal).
        available_lanes = [lane for lane in lanes if lane["kind"] != "local" and lane["available"]]
        if not available_lanes:
            return "held — no cloud backend reachable"

        total_cap = sum(lane["cap"] for lane in available_lanes)
        total_in_flight = sum(lane["in_flight"] for lane in available_lanes)
        free_slots = sum(max(0, lane["cap"] - lane["in_flight"]) for lane in available_lanes)
        if free_slots <= 0:
            return f"held — all lanes at capacity ({total_in_flight}/{total_cap} slots busy)"

        try:
            await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            return "held — no fileserver agent online"

        return f"queued — {free_slots} free slots, dispatching on next drain tick (~5 min)"
    except Exception:
        # Broad by design -- see the docstring above: ANY unexpected exception here (including one from
        # get_settings() or the fileserver probe) collapses to the neutral, no-causal-claim copy so the
        # hot 5s poll can never 500 on a hiccup (mirrors the T-71-03 idiom this module applies throughout).
        logger.warning("cloud_hold_reason_degraded", exc_info=True)
        return _HOLD_REASON_DEGRADED


async def get_analysis_live_count(session: AsyncSession, app_state: Any) -> int | None:
    """Return analyses executing now across local and configured cloud backends.

    Local execution is SAQ ``active``. Kueue execution is ``cloud_job.status == RUNNING``; SUBMITTED
    work is deliberately excluded because it can still be waiting for quota or admission. Compute has
    no equivalent execution signal, so any configured compute lane makes the aggregate unknown rather
    than silently contributing zero. This avoids availability probes, capacity reads, and serial
    per-lane history queries. If any source is unreadable or unobservable, the aggregate is unknown.
    """
    _, local_active = await _local_lane_queued_working(session, app_state)
    try:
        cloud_backends = [
            (backend, kind) for backend in resolve_backends(cast("ControlSettings", get_settings())) if (kind := _kind_of(backend)) != "local"
        ]
    except Exception:
        # Broad by design: get_settings()/resolve_backends() can fail on a malformed config in ways not
        # limited to a DB/SQLAlchemy error, and this aggregate's contract ("unknown" beats a fabricated
        # count) is the same degrade-safe posture the rest of this module applies to the hot poll.
        logger.warning("analysis_live_count_degraded", exc_info=True)
        return None
    if any(kind != "kueue" for _, kind in cloud_backends):
        return None
    cloud_ids = [backend.id for backend, _ in cloud_backends]
    if cloud_ids:
        cloud_active = await _safe_count_or_none(
            session,
            select(func.count(CloudJob.id)).where(CloudJob.status == CloudJobStatus.RUNNING.value, CloudJob.backend_id.in_(cloud_ids)),
            node="analysis_live_total",
        )
    else:
        cloud_active = 0
    if local_active is None or cloud_active is None:
        return None
    return local_active + cloud_active


async def _get_backend_routing_snapshot(session: AsyncSession, cfg: ControlSettings) -> list[dict[str, Any]] | None:
    """Return only the availability/capacity state the cloud hold gate consumes.

    Unlike :func:`get_backend_lane_snapshot`, this does not read admission buckets, queue depths, or
    processed history. ``None`` means degraded; an empty list is an observed empty registry.
    """
    try:
        backends = resolve_backends(cfg)
        availability = await _probe_availability(session, backends)
        await session.rollback()
        return [
            {
                "id": backend.id,
                "kind": _kind_of(backend),
                "cap": backend.cap,
                "in_flight": await backend.in_flight_count(session),
                "available": availability.get(backend.id, False),
            }
            for backend in backends
        ]
    except Exception:
        # Broad by design, mirrors get_backend_lane_snapshot's identical top-level guard: resolve_backends
        # + the pluggable-backend probe/in_flight_count calls above can raise heterogeneous errors, and
        # this feeds derive_cloud_hold_reason's own hot-poll degrade path (T-71-03).
        logger.warning("backend_routing_snapshot_degraded", exc_info=True)
        await _rollback_and_log(session, "backend_routing_snapshot_rollback_failed")
        return None
