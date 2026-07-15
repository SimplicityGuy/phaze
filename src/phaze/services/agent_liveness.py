"""Agent liveness classification (Phase 29 D-12 + UI-SPEC §Status Pill Component).

``classify``/``sort_key`` are pure functions — no DB, no I/O. The router
(``phaze.routers.admin_agents``) calls ``classify(agent, now)`` for every row and
injects the result on a transient ``agent._status`` attribute, then sorts the list
with ``sort_key(agent, now)`` before rendering. Tests and renderer share a single
source of truth via ``phaze.constants.AGENT_LIVENESS_*`` thresholds.

``classify_compute_lanes(session)`` (RECORD-03 / D-07) is the one DB-touching read
here — a degrade-safe, read-only ``CloudJob`` aggregation that models the ephemeral
k8s burst lane as an Active/Waiting/Idle Job-based identity (NEVER a perpetually-DEAD
agent). It mirrors the ``try/except → default`` count discipline in
``phaze.services.pipeline`` and lives beside ``classify`` because both answer the same
operator question ("what's alive right now?") for the two-section Agents page.

Status precedence (D-12 LOCKED):

    1. ``revoked``  — ``agent.revoked_at IS NOT NULL`` (takes precedence over
                      all ``last_seen_at`` math).
    2. ``never``    — ``revoked_at IS NULL AND last_seen_at IS NULL``.
    3. ``alive``    — ``now - last_seen_at < AGENT_LIVENESS_ALIVE_SECONDS`` (90s).
    4. ``stale``    — ``AGENT_LIVENESS_ALIVE_SECONDS <= delta
                       < AGENT_LIVENESS_STALE_SECONDS`` (90..300s).
    5. ``dead``     — ``delta >= AGENT_LIVENESS_STALE_SECONDS`` (>=300s).

Sort key (UI-SPEC LOCKED):

    ``(revoked_int, status_rank, -last_seen_unix_or_-inf)``

    - revoked agents land AFTER every non-revoked agent;
    - within non-revoked: ``alive (0) → stale (1) → dead (2) → never (3)``;
    - within the same status bucket: ``last_seen_at`` DESCENDING (more recent
      first) via the negated unix-timestamp tiebreaker.

Import-boundary note: importing ``phaze.models.agent`` IS allowed here. The
Postgres-free invariant applies only to ``phaze.cert_bootstrap``,
``phaze.entrypoint``, ``phaze.tasks.agent_worker``, and ``phaze.tasks._shared.*``
— NOT to ``phaze.services.*``.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
import structlog

from phaze.config import get_settings
from phaze.constants import AGENT_LIVENESS_ALIVE_SECONDS, AGENT_LIVENESS_STALE_SECONDS
from phaze.models.cloud_job import CloudJob, CloudJobStatus


if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.models.agent import Agent


logger = structlog.get_logger(__name__)


AgentStatus = Literal["alive", "stale", "dead", "revoked", "never"]
"""5 LOCKED status values per UI-SPEC §Status Pill Component (Phase 29 D-12)."""

_STATUS_RANK: dict[AgentStatus, int] = {
    "alive": 0,
    "stale": 1,
    "dead": 2,
    "revoked": 3,
    "never": 3,
}
"""Sort-rank inside the non-revoked group: alive=0 → stale=1 → dead=2 → never=3.

'revoked' has rank 3 too but is dominated by the leading 'revoked_int' tier in
``sort_key`` — so its rank never decides ordering against non-revoked agents.
'never' shares rank 3 with 'revoked' because both represent "no signal", but
'never' agents stay in the non-revoked group so they appear above any revoked
row in the rendered table.
"""


def classify(agent: Agent, now: datetime) -> AgentStatus:
    """Return the 5-state liveness label for ``agent`` evaluated at ``now``.

    Precedence (D-12 LOCKED): revoked → never → alive/stale/dead by threshold.

    The ``now`` parameter is explicit (not ``datetime.now()`` inside the body)
    so tests are time-deterministic without freezegun. Mirrors the
    ``elapsed_seconds(batch)`` shape in ``phaze.routers.pipeline_scans``.
    """
    if agent.revoked_at is not None:
        return "revoked"
    if agent.last_seen_at is None:
        return "never"
    delta_seconds = (now - agent.last_seen_at).total_seconds()
    if delta_seconds < AGENT_LIVENESS_ALIVE_SECONDS:
        return "alive"
    if delta_seconds < AGENT_LIVENESS_STALE_SECONDS:
        return "stale"
    return "dead"


def sort_key(agent: Agent, now: datetime) -> tuple[int, int, float]:
    """Return the sort tuple for ``agent`` at ``now`` (UI-SPEC LOCKED order).

    Tuple shape: ``(revoked_int, status_rank, -last_seen_unix_or_-inf)``.

    - ``revoked_int`` is 1 for revoked agents, 0 otherwise. Sorted ascending,
      so non-revoked agents (0) come before revoked agents (1).
    - ``status_rank`` is the entry in ``_STATUS_RANK`` for ``classify(agent, now)``.
      Sorted ascending so 'alive' (0) → 'stale' (1) → 'dead' (2) → 'never' (3).
    - The tiebreaker is the NEGATED unix timestamp of ``last_seen_at`` so
      more-recently-seen agents sort first. Agents with ``last_seen_at IS NULL``
      tie at ``-inf`` (negation of ``+inf``) — they land at the END of their
      bucket, which only matters for the 'never' bucket (revoked agents with
      NULL last_seen still get the float fallback but never compete inside the
      non-revoked group).
    """
    revoked_int = 1 if agent.revoked_at is not None else 0
    status = classify(agent, now)
    status_rank = _STATUS_RANK[status]
    # Agents with last_seen_at IS NULL land at the END of their bucket via +inf
    # (negation of -inf would be ambiguous; +inf is the largest finite-or-inf
    # value so ascending sort puts these rows last within the bucket). Only
    # the 'never' bucket actually exercises this path inside the non-revoked
    # group.
    neg_last_seen = math.inf if agent.last_seen_at is None else -agent.last_seen_at.timestamp()
    return (revoked_int, status_rank, neg_last_seen)


ComputeLaneState = Literal["ACTIVE", "WAITING", "IDLE"]
"""3-state liveness for the k8s burst lane (RECORD-03 / D-07). DEAD is NEVER a member.

The Kubernetes burst lane is modeled as an ephemeral, Job-based identity — NOT a
heartbeating agent — so it can never be "perpetually DEAD". Its liveness is derived
live from in-flight ``CloudJob`` counts and degrades to ``IDLE`` (never DEAD/red) on
any DB error (KDEPLOY-04).
"""


@dataclass(frozen=True)
class ComputeLane:
    """One derived compute-lane identity for the two-section Agents page (COMPUTE-01).

    A per-cluster liveness identity composed from the Phase-67 backend registry (one lane per
    NON-local entry) and the live in-flight ``CloudJob`` counts attributed to that backend. A lane
    is NEVER a heartbeating agent — its ``state`` is derived purely from in-flight work (``running`` /
    ``waiting``), so a configured-but-quiet cluster is ``IDLE`` (listed, never DEAD/red) and a DB
    hiccup degrades every lane to ``IDLE`` rather than raising into the hot poll (KDEPLOY-04).
    """

    backend_id: str
    kind: str
    state: ComputeLaneState
    running: int
    waiting: int


def _lane_state(running: int, waiting: int) -> ComputeLaneState:
    """Return the 3-state lane liveness by precedence: running≥1 → ACTIVE, waiting≥1 → WAITING, else IDLE.

    DEAD is structurally impossible here (KDEPLOY-04): a compute lane is an ephemeral Job-based
    identity, so quiescence is ``IDLE`` (green/neutral), never a perpetually-DEAD pill.
    """
    if running >= 1:
        return "ACTIVE"
    if waiting >= 1:
        return "WAITING"
    return "IDLE"


def non_local_backend_kinds(settings: ControlSettings) -> dict[str, str]:
    """Return ``{backend_id: kind}`` for every registry entry whose ``kind != "local"`` (COMPUTE-01).

    A pure, session-free projection of the Phase-67 registry (``settings.backends``) — the shared
    helper the per-cluster lane derivation here and the later header-count / file-badge beads all
    consume so "which backends are cloud lanes?" is answered in exactly one place. Insertion order
    mirrors ``settings.backends`` so downstream lane ordering is registry-deterministic.
    """
    return {backend.id: backend.kind for backend in settings.backends if backend.kind != "local"}


async def derive_compute_lane_identities(session: AsyncSession) -> list[ComputeLane]:
    """Return one :class:`ComputeLane` per non-local registry backend + a trailing unattributed lane (COMPUTE-01).

    Composes the Phase-67 registry (``get_settings().backends``, non-local entries) with a SINGLE
    grouped ``CloudJob`` read (``GROUP BY backend_id`` with filtered counts — ``RUNNING`` → running,
    ``SUBMITTED AND inadmissible`` → waiting), mirroring the ``_admission_by_backend_id`` idiom in
    ``services.backends``. Every configured cluster appears even when IDLE (0 counts); liveness is
    in-flight WORK, never a reachability probe. In-flight rows with a NULL ``backend_id`` collapse
    into ONE trailing ``"unattributed"``/``kind="cloud"`` lane, emitted only when its counts are
    non-zero.

    Degrade-safe (KDEPLOY-04): a :class:`~sqlalchemy.exc.SQLAlchemyError` rolls the session back and
    returns the registry lanes all-``IDLE`` (a DB hiccup must NEVER paint a lane DEAD/red); a
    settings/registry read failure returns ``[]``. This must never raise on the hot poll path.
    """
    try:
        kinds = non_local_backend_kinds(cast("ControlSettings", get_settings()))
    except Exception:
        logger.warning("compute_lane_identity_registry_unavailable", exc_info=True)
        return []

    try:
        stmt = select(
            CloudJob.backend_id,
            func.count().filter(CloudJob.status == CloudJobStatus.RUNNING.value).label("running"),
            func.count().filter(CloudJob.status == CloudJobStatus.SUBMITTED.value, CloudJob.inadmissible.is_(True)).label("waiting"),
        ).group_by(CloudJob.backend_id)
        rows = (await session.execute(stmt)).all()
    except SQLAlchemyError:
        logger.warning("compute_lane_identity_degraded", exc_info=True)
        try:
            await session.rollback()
        except SQLAlchemyError:
            logger.warning("compute_lane_identity_rollback_failed", exc_info=True)
        return [ComputeLane(backend_id=backend_id, kind=kind, state="IDLE", running=0, waiting=0) for backend_id, kind in kinds.items()]

    counts = {backend_id: (int(running or 0), int(waiting or 0)) for backend_id, running, waiting in rows}

    lanes: list[ComputeLane] = []
    for backend_id, kind in kinds.items():
        running, waiting = counts.get(backend_id, (0, 0))
        lanes.append(ComputeLane(backend_id=backend_id, kind=kind, state=_lane_state(running, waiting), running=running, waiting=waiting))

    null_running, null_waiting = counts.get(None, (0, 0))
    if null_running or null_waiting:
        lanes.append(
            ComputeLane(
                backend_id="unattributed", kind="cloud", state=_lane_state(null_running, null_waiting), running=null_running, waiting=null_waiting
            )
        )

    return lanes


async def classify_compute_lanes(session: AsyncSession) -> tuple[str, int]:
    """Return the aggregate compute-lane liveness state + in-flight count (RECORD-03, D-07).

    Thin backward-compat shim over :func:`derive_compute_lane_identities` (COMPUTE-01): it collapses
    the per-cluster lanes back to the single ``(state, count)`` contract the existing router / template
    callers still consume, so this bead ships template-free. It is removed in the follow-up tiles bead
    once those callers render per-lane identities directly. Precedence over the aggregated counts:

    - ``("ACTIVE", running)`` when ≥1 lane has a ``running`` job — the burst lane is doing work;
    - ``("WAITING", waiting)`` when nothing is running but ≥1 is ``submitted`` AND ``inadmissible``
      (blocked behind a misconfigured Kueue quota);
    - ``("IDLE", 0)`` when nothing is in-flight.

    Degrade-safe (T-61-08 / KDEPLOY-04): :func:`derive_compute_lane_identities` already rolls back and
    returns all-``IDLE`` (or ``[]``) on any DB / registry error, so the aggregate collapses to
    ``("IDLE", 0)`` — a DB hiccup must NEVER paint the lane DEAD/red (a false alarm).
    """
    lanes = await derive_compute_lane_identities(session)
    running = sum(lane.running for lane in lanes)
    waiting = sum(lane.waiting for lane in lanes)
    if running >= 1:
        return ("ACTIVE", running)
    if waiting >= 1:
        return ("WAITING", waiting)
    return ("IDLE", 0)
