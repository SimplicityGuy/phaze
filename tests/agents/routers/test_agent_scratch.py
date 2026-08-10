"""phaze-5cvbz tests for POST /api/internal/agent/scratch/live.

Uses an inline smoke FastAPI app builder (mirrors ``test_agent_heartbeat.py``) so this suite
is parallel-safe and does not depend on ``main.py``'s router registration order. ``saq_jobs``
is SAQ-owned (not Alembic-managed) so it is created inline per test, matching the
``tests/analyze/tasks/test_active_reaper.py`` precedent -- the same table
``get_live_job_keys`` (and therefore this endpoint) reads.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from phaze.database import get_session
from phaze.routers.agent_scratch import router as agent_scratch_router


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_CREATE_SAQ_JOBS = text(
    """
    CREATE TABLE IF NOT EXISTS saq_jobs (
        key TEXT PRIMARY KEY,
        lock_key SERIAL NOT NULL,
        job BYTEA NOT NULL,
        queue TEXT NOT NULL,
        status TEXT NOT NULL,
        priority SMALLINT NOT NULL DEFAULT 0,
        group_key TEXT,
        scheduled BIGINT NOT NULL DEFAULT 0,
        expire_at BIGINT
    )
    """
)


async def _seed_job(session: AsyncSession, *, key: str, status: str, queue: str = "phaze-agent-nox") -> None:
    await session.execute(
        text("INSERT INTO saq_jobs (key, job, queue, status, scheduled) VALUES (:key, :job, :queue, :status, 0)"),
        {"key": key, "job": json.dumps({"key": key}).encode("utf-8"), "queue": queue, "status": status},
    )
    await session.commit()


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_scratch_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _post_liveness(session: AsyncSession, token: str, file_ids: list[uuid.UUID]) -> dict:
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post(
            "/api/internal/agent/scratch/live",
            json={"file_ids": [str(fid) for fid in file_ids]},
        )
    assert response.status_code == 200
    return response.json()


async def test_liveness_reports_queued_process_file_job_as_live(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A QUEUED process_file job (never yet dequeued) is reported live (phaze-5cvbz core scenario)."""
    _agent, token = seed_test_agent
    await session.execute(_CREATE_SAQ_JOBS)
    live_id = uuid.uuid4()
    await _seed_job(session, key=f"process_file:{live_id}", status="queued")

    body = await _post_liveness(session, token, [live_id])

    assert body["live_file_ids"] == [str(live_id)]
    assert body["push_file_active"] is False


async def test_liveness_reports_active_process_file_job_as_live(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    _agent, token = seed_test_agent
    await session.execute(_CREATE_SAQ_JOBS)
    live_id = uuid.uuid4()
    await _seed_job(session, key=f"process_file:{live_id}", status="active")

    body = await _post_liveness(session, token, [live_id])

    assert body["live_file_ids"] == [str(live_id)]


async def test_liveness_excludes_terminal_process_file_job(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A COMPLETE/FAILED row is not live -- the entry is safe to sweep."""
    _agent, token = seed_test_agent
    await session.execute(_CREATE_SAQ_JOBS)
    done_id = uuid.uuid4()
    await _seed_job(session, key=f"process_file:{done_id}", status="complete")

    body = await _post_liveness(session, token, [done_id])

    assert body["live_file_ids"] == []


async def test_liveness_excludes_file_id_with_no_job_at_all(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    _agent, token = seed_test_agent
    await session.execute(_CREATE_SAQ_JOBS)

    body = await _post_liveness(session, token, [uuid.uuid4()])

    assert body["live_file_ids"] == []


async def test_liveness_only_returns_requested_ids(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A live job for a file_id NOT in the request must not leak into the response."""
    _agent, token = seed_test_agent
    await session.execute(_CREATE_SAQ_JOBS)
    live_id = uuid.uuid4()
    other_live_id = uuid.uuid4()
    await _seed_job(session, key=f"process_file:{live_id}", status="queued")
    await _seed_job(session, key=f"process_file:{other_live_id}", status="queued")

    body = await _post_liveness(session, token, [live_id])

    assert body["live_file_ids"] == [str(live_id)]


async def test_liveness_push_file_active_ignores_requested_file_ids(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """push_file_active is coarse -- True whenever ANY push_file job is live, independent of file_ids."""
    _agent, token = seed_test_agent
    await session.execute(_CREATE_SAQ_JOBS)
    push_file_id = uuid.uuid4()
    await _seed_job(session, key=f"push_file:{push_file_id}", status="active")

    body = await _post_liveness(session, token, [uuid.uuid4()])

    assert body["live_file_ids"] == []
    assert body["push_file_active"] is True


async def test_liveness_empty_file_ids_still_reports_push_file_active(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    _agent, token = seed_test_agent
    await session.execute(_CREATE_SAQ_JOBS)
    push_file_id = uuid.uuid4()
    await _seed_job(session, key=f"push_file:{push_file_id}", status="queued")

    body = await _post_liveness(session, token, [])

    assert body["live_file_ids"] == []
    assert body["push_file_active"] is True


async def test_liveness_rejects_unauthenticated_request(session: AsyncSession) -> None:
    """Missing Authorization header -> 401 (HTTPBearer auto_error), per agent_auth.py D-06."""
    app = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/api/internal/agent/scratch/live", json={"file_ids": []})
    assert response.status_code == 401


async def test_liveness_rejects_too_many_file_ids(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """extra=forbid + max_length caps the request -- a runaway scratch dir can't build an unbounded query."""
    _agent, token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        response = await ac.post(
            "/api/internal/agent/scratch/live",
            json={"file_ids": [str(uuid.uuid4()) for _ in range(501)]},
        )
    assert response.status_code == 422
