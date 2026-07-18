"""phaze-30fo — per-lane heartbeats so one stalled lane cannot paint a busy agent DEAD.

The 2026-07-18 nox incident: /admin/agents showed "nox DEAD, queue 762" while nox's
fingerprint lane was completing a job every ~2.6 seconds. Liveness came from the analyze
worker alone, and `Agent.last_seen_at` is not just a display field -- it is the key
`enqueue_router.select_active_agent` orders by, so the busiest machine in the fleet also
lost work routing.

The headline test here is `test_busy_lane_keeps_agent_alive_while_analyze_is_stopped`,
which reproduces exactly that scenario.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import update

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_heartbeat import router as agent_heartbeat_router
from phaze.services.agent_liveness import classify


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_heartbeat_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _beat(lane: str | None, depth: int, pid: int = 1234) -> dict[str, object]:
    payload: dict[str, object] = {"agent_version": "4.0.0", "worker_pid": pid, "queue_depth": depth}
    if lane is not None:
        payload["lane"] = lane
    return payload


async def _post(session: AsyncSession, token: str, payload: dict[str, object]) -> int:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post("/api/internal/agent/heartbeat", json=payload)
    return response.status_code


@pytest.mark.asyncio
async def test_busy_lane_keeps_agent_alive_while_analyze_is_stopped(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """THE acceptance scenario: analyze stopped long ago, fingerprint still working -> NOT dead.

    Before phaze-30fo only the analyze worker beat, so `last_seen_at` would still be the
    stale timestamp below and classify() would return "dead" while the agent was busy.
    """
    agent, token = seed_test_agent

    # Analyze lane last beat 20 minutes ago -- far past the 300s DEAD threshold.
    stale = datetime.now(UTC) - timedelta(minutes=20)
    await session.execute(update(Agent).where(Agent.id == agent.id).values(last_seen_at=stale))
    await session.commit()
    await session.refresh(agent)
    assert classify(agent, datetime.now(UTC)) == "dead", "precondition: agent reads DEAD on the analyze beat alone"

    # The fingerprint lane is alive and working -- it now beats on its own behalf.
    assert await _post(session, token, _beat("fingerprint", 11290)) == 204

    await session.refresh(agent)
    assert classify(agent, datetime.now(UTC)) == "alive", "a working lane must keep the agent alive (phaze-30fo)"


@pytest.mark.asyncio
async def test_queue_depth_is_summed_across_lanes(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The admin QUEUE column must reflect ALL lanes, not just analyze.

    The incident showed "queue 762" (analyze only) while the agent held ~12k queued jobs.
    The template renders last_status['queue_depth'] unchanged, so the sum lands there.
    """
    agent, token = seed_test_agent

    assert await _post(session, token, _beat("analyze", 762)) == 204
    assert await _post(session, token, _beat("fingerprint", 11290)) == 204
    assert await _post(session, token, _beat("meta", 3)) == 204
    assert await _post(session, token, _beat("io", 0)) == 204

    await session.refresh(agent)
    assert agent.last_status is not None
    assert agent.last_status["queue_depth"] == 762 + 11290 + 3, "top-level depth must be the cross-lane SUM"
    # Per-lane breakdown is retained so an operator can see WHICH lane holds the backlog.
    assert agent.last_status["lanes"]["analyze"]["queue_depth"] == 762
    assert agent.last_status["lanes"]["fingerprint"]["queue_depth"] == 11290
    assert set(agent.last_status["lanes"]) == {"analyze", "fingerprint", "meta", "io"}


@pytest.mark.asyncio
async def test_relaying_one_lane_does_not_drop_the_others(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A later beat updates its OWN lane and leaves the rest intact (merge, not replace)."""
    agent, token = seed_test_agent

    assert await _post(session, token, _beat("analyze", 100)) == 204
    assert await _post(session, token, _beat("fingerprint", 200)) == 204
    # Analyze drains; fingerprint's entry must survive.
    assert await _post(session, token, _beat("analyze", 0)) == 204

    await session.refresh(agent)
    assert agent.last_status is not None
    assert agent.last_status["lanes"]["analyze"]["queue_depth"] == 0
    assert agent.last_status["lanes"]["fingerprint"]["queue_depth"] == 200
    assert agent.last_status["queue_depth"] == 200


@pytest.mark.asyncio
async def test_unlaned_beat_is_still_accepted_and_stored_verbatim(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Rolling-deploy safety: an older agent posts no `lane` and must NOT be rejected.

    A required lane field would 422 every beat from a not-yet-upgraded agent, turning a
    liveness fix into a liveness outage mid-deploy.
    """
    agent, token = seed_test_agent

    assert await _post(session, token, _beat(None, 5)) == 204

    await session.refresh(agent)
    # Byte-identical to the pre-phaze-30fo shape -- no stray `lane: null` key.
    assert agent.last_status == {"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 5}
    assert agent.last_seen_at is not None
