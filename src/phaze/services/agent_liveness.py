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

import math
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
import structlog

from phaze.constants import AGENT_LIVENESS_ALIVE_SECONDS, AGENT_LIVENESS_STALE_SECONDS
from phaze.models.cloud_job import CloudJob, CloudJobStatus


if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

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


async def classify_compute_lanes(session: AsyncSession) -> tuple[str, int]:
    """Return the ephemeral compute-lane liveness state + in-flight count (RECORD-03, D-07).

    Read-only ``CloudJob`` aggregation — mirrors the degrade-safe ``try/except → default``
    count shape of :func:`phaze.services.pipeline.get_inadmissible_count` /
    :func:`phaze.services.pipeline.get_cloud_phase_counts`. Precedence:

    - ``("ACTIVE", running)`` when ≥1 ``CloudJob.status == running`` — the lane is doing work;
    - ``("WAITING", waiting)`` when no job is running but ≥1 is ``submitted`` AND
      ``inadmissible`` (blocked behind a misconfigured Kueue quota);
    - ``("IDLE", 0)`` when nothing is in-flight.

    Degrade-safe (T-61-08 / KDEPLOY-04): on any :class:`~sqlalchemy.exc.SQLAlchemyError`
    the session is rolled back and the lane degrades to ``("IDLE", 0)`` — a DB hiccup
    must NEVER paint the lane DEAD/red (a false alarm). This is a pure aggregation the
    router injects on the render (never a perpetually-DEAD agent row).
    """
    try:
        running = int((await session.execute(select(func.count(CloudJob.id)).where(CloudJob.status == CloudJobStatus.RUNNING.value))).scalar() or 0)
        waiting = int(
            (
                await session.execute(
                    select(func.count(CloudJob.id)).where(
                        CloudJob.status == CloudJobStatus.SUBMITTED.value,
                        CloudJob.inadmissible.is_(True),
                    )
                )
            ).scalar()
            or 0
        )
    except SQLAlchemyError:
        logger.warning("compute_lane_liveness_degraded", exc_info=True)
        try:
            await session.rollback()
        except SQLAlchemyError:
            logger.warning("compute_lane_liveness_rollback_failed", exc_info=True)
        return ("IDLE", 0)

    if running >= 1:
        return ("ACTIVE", running)
    if waiting >= 1:
        return ("WAITING", waiting)
    return ("IDLE", 0)
