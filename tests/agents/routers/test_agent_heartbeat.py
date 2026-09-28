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
from phaze.runtime_config import RuntimeConfig
from phaze.schemas.agent_heartbeat import EffectiveConfig, EffectiveConfigLastReload, HeartbeatRequest


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_PAYLOAD = {"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 5}

_RUNTIME_CONFIG_VALUES = RuntimeConfig(
    log_level="DEBUG",
    worker_max_jobs=4,
    lane_analyze_concurrency=2,
    lane_meta_concurrency=2,
    lane_io_concurrency=2,
    worker_process_pool_size=2,
    analysis_intra_op_threads=1,
    analysis_omp_threads=1,
    analysis_stall_timeout_sec=1800,
    cloud_route_threshold_sec=3600,
)


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
async def test_heartbeat_over_limit_queue_depth_422_not_500(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-s4r0: an out-of-domain queue_depth must 422 at the wire boundary, never reach the DB.

    Before the Field(le=QUEUE_DEPTH_MAX) bound, a value like this survived Pydantic (an unbounded
    Python int), then blew up _LANE_MERGE_SQL's `::bigint` cast with NumericValueOutOfRange -- an
    unhandled 500. The fix rejects it before a transaction is even opened.
    """
    _agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post(
            "/api/internal/agent/heartbeat",
            json={"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": 9999999999999999999, "lane": "analyze"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_heartbeat_negative_queue_depth_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """queue_depth is a count -- a negative value is nonsense and must 422, not silently persist."""
    _agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post(
            "/api/internal/agent/heartbeat",
            json={"agent_version": "4.0.0", "worker_pid": 1234, "queue_depth": -1},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_heartbeat_out_of_range_worker_pid_422(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """worker_pid is a positive pid -- zero/negative or past int32 must 422 (defense-in-depth bound)."""
    _agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post(
            "/api/internal/agent/heartbeat",
            json={"agent_version": "4.0.0", "worker_pid": 0, "queue_depth": 5},
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


@pytest.mark.asyncio
async def test_heartbeat_with_effective_config_is_accepted_and_persisted(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-mvq8z.9: the NEW shape -- an agent reporting its resolved reloadable-config snapshot
    on the SAME beat -- is accepted and lands, in full, in `last_status`.
    """
    agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    request = HeartbeatRequest(
        agent_version="5.0.0",
        worker_pid=1234,
        queue_depth=5,
        effective_config=EffectiveConfig(
            values=_RUNTIME_CONFIG_VALUES,
            sources=dict.fromkeys(RuntimeConfig.model_fields, "env"),
            restart_only_keys=["database_url"],
            last_reload=EffectiveConfigLastReload(source="startup", outcome="applied", error=None, at=1234.5),
        ),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post("/api/internal/agent/heartbeat", json=request.model_dump(mode="json"))
    assert response.status_code == 204

    await session.refresh(agent)
    assert agent.last_status is not None
    stored = agent.last_status["effective_config"]
    assert stored["values"]["worker_max_jobs"] == 4
    assert stored["sources"]["worker_max_jobs"] == "env"
    assert stored["restart_only_keys"] == ["database_url"]
    assert stored["last_reload"] == {"source": "startup", "outcome": "applied", "error": None, "at": 1234.5}


@pytest.mark.asyncio
async def test_old_shape_heartbeat_without_effective_config_omits_the_key_entirely(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """phaze-mvq8z.9's own blast-radius proof (bead acceptance: "an old-shape heartbeat is still
    accepted"): a payload with no `effective_config` at all -- an agent built before this bead --
    is accepted AND stores no stray `effective_config: null` key, extending the SAME byte-identical
    guarantee `test_heartbeat_persists_status` above already relies on for `_PAYLOAD`.
    """
    agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post("/api/internal/agent/heartbeat", json=_PAYLOAD)
    assert response.status_code == 204

    await session.refresh(agent)
    assert agent.last_status == _PAYLOAD
    assert "effective_config" not in agent.last_status
