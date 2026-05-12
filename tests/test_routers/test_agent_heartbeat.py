"""DIST-04 (5/5) + D-17 + D-19 + AUTH-04 tests for POST /api/internal/agent/heartbeat.

Uses an inline smoke FastAPI app builder (mirrors test_agent_auth.py) because Plan 06
wires the agent_heartbeat router into `main.py`; this test suite is parallel-safe
and does not depend on Plans 03/05/06 landing in any particular order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.sql import func as sa_func

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.agent_heartbeat import router as agent_heartbeat_router


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_PAYLOAD = {"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 5}


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a small FastAPI app that wires the agent_heartbeat router."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_heartbeat_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.mark.asyncio
async def test_heartbeat_persists_status(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """DIST-04 (5/5): heartbeat persists payload to agents.last_status JSONB AND stamps last_seen_at."""
    agent, raw_token = seed_test_agent

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post("/api/internal/agent/heartbeat", json=_PAYLOAD)

    assert response.status_code == 204
    assert response.content == b""  # D-19 -- no body

    await session.refresh(agent)
    assert agent.last_status == _PAYLOAD
    assert agent.last_seen_at is not None


@pytest.mark.asyncio
async def test_heartbeat_returns_204(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """D-19 explicit: heartbeat returns 204 with NO body."""
    _agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post("/api/internal/agent/heartbeat", json=_PAYLOAD)
    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.asyncio
async def test_heartbeat_missing_field_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """D-17: HeartbeatRequest requires all three fields; missing queue_depth -> 422."""
    _agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post(
            "/api/internal/agent/heartbeat",
            json={"agent_version": "4.0.0", "worker_pid": 1234},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_heartbeat_revoke_blocks_next_call(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """AUTH-04 reaffirmed on production route: revoke between calls -> next call returns 403."""
    agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r1 = await ac.post("/api/internal/agent/heartbeat", json=_PAYLOAD)
        assert r1.status_code == 204

        await session.execute(update(Agent).where(Agent.id == agent.id).values(revoked_at=sa_func.now()))
        await session.commit()

        r2 = await ac.post("/api/internal/agent/heartbeat", json=_PAYLOAD)
        assert r2.status_code == 403
