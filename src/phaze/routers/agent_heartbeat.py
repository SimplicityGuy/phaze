"""POST /api/internal/agent/heartbeat -- agent liveness signal (phase-25 D-17, D-19).

phaze-30fo: liveness used to be pinned to ONE lane. Compose set PHAZE_AGENT_HEARTBEAT=true
on exactly the analyze-lane worker, so `agents.last_seen_at` -- the ONLY liveness signal --
came from a single process. When that process stalled, the agent was classified DEAD after
300s while its other three lanes were actively processing work. Observed live on
2026-07-18: /admin/agents showed "nox DEAD, queue 762" while nox's fingerprint lane was
completing a job every ~2.6s.

That was never only a display bug. `Agent.last_seen_at` is also the WORK-ROUTING key --
`enqueue_router.select_active_agent` orders by `last_seen_at DESC` -- so a stale heartbeat
sorted the busiest machine in the fleet to the bottom and cost it work routing.

Every lane now heartbeats with a `lane` tag. Two consequences, both handled here:

  * `last_seen_at` is refreshed by ANY lane's beat. Since it is always set to `now()`,
    it is inherently max(last_seen) across lanes -- no explicit GREATEST needed. One
    stalled lane can no longer paint the whole agent DEAD.
  * `last_status` keeps a per-lane breakdown under `lanes`, and the TOP-LEVEL
    `queue_depth` becomes the SUM across lanes. The admin table already renders
    `last_status['queue_depth']`, so that column silently goes from "analyze lane only"
    (the misleading 762) to the agent's true all-lane total, with no template change.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_heartbeat import HeartbeatRequest


router = APIRouter(prefix="/api/internal/agent/heartbeat", tags=["agent-internal"])


_LANE_MERGE_SQL = text(
    """
    UPDATE agents
    SET last_seen_at = now(),
        last_status = merged.js || jsonb_build_object(
            'queue_depth',
            COALESCE(
                (SELECT SUM((v ->> 'queue_depth')::bigint)
                 FROM jsonb_each(merged.js -> 'lanes') AS e(k, v)),
                0
            )
        )
    FROM (
        SELECT COALESCE(a.last_status, '{}'::jsonb)
               || CAST(:base AS jsonb)
               || jsonb_build_object(
                      'lanes',
                      COALESCE(a.last_status -> 'lanes', '{}'::jsonb)
                      || jsonb_build_object(CAST(:lane AS text), CAST(:lane_status AS jsonb))
                  ) AS js
        FROM agents a
        WHERE a.id = :agent_id
    ) AS merged
    WHERE agents.id = :agent_id
    """,
)
"""Merge one lane's beat and recompute the summed depth, atomically.

Deliberately ONE statement rather than a read-modify-write in Python. Four lanes beat
concurrently (~4 writes/30s per agent); a Python-side merge would lose updates whenever
two beats interleave, silently dropping a lane from the breakdown until its next tick.

Uses explicit `||` object merging rather than `jsonb_set(..., ARRAY['lanes', lane], v, true)`:
`create_missing` only creates the FINAL key, never intermediate ones, so on the first beat
(when `lanes` does not yet exist) jsonb_set is a silent no-op and every depth reads 0.
"""


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def post_heartbeat(
    body: HeartbeatRequest,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Update agents.last_seen_at and last_status. Returns 204 No Content (D-19).

    A beat carrying `lane` is merged into the per-lane breakdown; an unlaned beat (older
    agent image, or all-mode where there is no lane split) is persisted verbatim exactly
    as it always was, so a rolling deploy degrades cleanly in both directions.
    """
    if body.lane is None:
        # `exclude={"lane"}` keeps the stored shape byte-identical to the pre-phaze-30fo
        # payload rather than adding a `lane: null` key. An unlaned agent's last_status
        # should look exactly as it always did.
        await session.execute(
            update(Agent).where(Agent.id == agent.id).values(last_seen_at=func.now(), last_status=body.model_dump(exclude={"lane"})),
        )
    else:
        payload = body.model_dump()
        # Top-level fields describe the most recent beat; `queue_depth` is excluded here
        # because the SQL replaces it with the cross-lane SUM.
        base = {k: v for k, v in payload.items() if k != "queue_depth"}
        lane_status = {k: v for k, v in payload.items() if k != "lane"}
        await session.execute(
            _LANE_MERGE_SQL,
            {
                "agent_id": agent.id,
                "lane": body.lane,
                "base": json.dumps(base),
                "lane_status": json.dumps(lane_status),
            },
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
