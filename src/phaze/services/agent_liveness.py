"""Agent liveness classification (Phase 29 D-12 + UI-SPEC §Status Pill Component).

Pure functions only — no DB, no I/O. The router (``phaze.routers.admin_agents``)
calls ``classify(agent, now)`` for every row and injects the result on a
transient ``agent._status`` attribute, then sorts the list with
``sort_key(agent, now)`` before rendering. Tests and renderer share a single
source of truth via ``phaze.constants.AGENT_LIVENESS_*`` thresholds.

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

from phaze.constants import AGENT_LIVENESS_ALIVE_SECONDS, AGENT_LIVENESS_STALE_SECONDS


if TYPE_CHECKING:
    from datetime import datetime

    from phaze.models.agent import Agent


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
