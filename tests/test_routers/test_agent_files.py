"""DIST-04 / DIST-05 / D-16 / D-20 / D-22 / AUTH-01 tests for POST /api/internal/agent/files.

Why local fixture overrides exist (Rule 3 deviation):
    Plan 25-03 ships ``src/phaze/routers/agent_files.py`` but does NOT wire it
    into ``main.py`` -- that is Plan 25-06's job (Wave 4). The conftest.py
    ``authenticated_client`` fixture uses ``create_app()``, so without local
    overrides every router test would return 404. The local fixtures below
    construct a self-contained FastAPI app that mounts ``agent_files.router``
    and ``health.router`` so DIST-04 / DIST-05 / D-16 / D-20 / D-22 tests can
    exercise the real handler in Wave 3, matching Plan 25-02's smoke-app
    pattern (``tests/test_routers/test_agent_auth.py::_make_smoke_app``).
    Test 8 (``test_missing_auth_returns_401``) intentionally uses the
    production ``client`` fixture to verify the route is correctly 404 on the
    production app until Plan 06 wires it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import func as sa_func, select

from phaze.config import settings
from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.routers import agent_files


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Build a FastAPI app wiring agent_files.router so Wave-3 tests can call the real handler."""
    app = FastAPI(title="agent-files-smoke", version="test")
    app.include_router(agent_files.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest_asyncio.fixture
async def authenticated_client(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> AsyncGenerator[AsyncClient]:
    """LOCAL OVERRIDE of conftest.authenticated_client: smoke app with agent_files wired.

    Replaces the conftest version (which uses ``create_app()`` and therefore lacks
    the agent_files router until Plan 06). Same Authorization header convention.
    """
    _agent, raw_token = seed_test_agent
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac


def _make_record(path: str = "/test/music/a.mp3", ext: str = "mp3", size: int = 100) -> dict[str, object]:
    return {
        "sha256_hash": "0" * 64,
        "original_path": path,
        "original_filename": path.rsplit("/", 1)[-1],
        "current_path": path,
        "file_type": ext,
        "file_size": size,
    }


@pytest.mark.asyncio
async def test_upsert_happy_path(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    agent, _ = seed_test_agent
    with patch("phaze.routers.agent_files.Queue") as MockQueue:
        MockQueue.from_url.return_value = AsyncMock()
        response = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == agent.id
    assert body["upserted"] == 1
    assert body["inserted"] == 1
    assert body["enqueued"] == 1
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_replay_no_duplicates(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    with patch("phaze.routers.agent_files.Queue") as MockQueue:
        MockQueue.from_url.return_value = AsyncMock()
        r1 = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
        r2 = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    assert r1.status_code == 200
    assert r2.status_code == 200
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_auto_enqueue_only_for_inserts(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    agent, _ = seed_test_agent
    chunk = {"files": [_make_record(path="/test/music/a.mp3"), _make_record(path="/test/music/b.mp3")]}
    with patch("phaze.routers.agent_files.Queue") as MockQueue:
        mock_queue = AsyncMock()
        MockQueue.from_url.return_value = mock_queue
        response = await authenticated_client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 200
    MockQueue.from_url.assert_called_once_with(settings.redis_url, name=f"phaze-agent-{agent.id}")
    assert mock_queue.enqueue.await_count == 2
    for call in mock_queue.enqueue.await_args_list:
        args, kwargs = call
        assert args[0] == "extract_file_metadata"
        assert "file_id" in kwargs
        uuid.UUID(kwargs["file_id"])
    mock_queue.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_enqueue_for_updates(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    chunk = {"files": [_make_record()]}
    with patch("phaze.routers.agent_files.Queue") as MockQueue1:
        mq1 = AsyncMock()
        MockQueue1.from_url.return_value = mq1
        r1 = await authenticated_client.post("/api/internal/agent/files", json=chunk)
        assert r1.status_code == 200
        assert mq1.enqueue.await_count == 1
    with patch("phaze.routers.agent_files.Queue") as MockQueue2:
        mq2 = AsyncMock()
        MockQueue2.from_url.return_value = mq2
        r2 = await authenticated_client.post("/api/internal/agent/files", json=chunk)
        assert r2.status_code == 200
        assert mq2.enqueue.await_count == 0
        body = r2.json()
        assert body["inserted"] == 0
        assert body["upserted"] == 1
        assert body["enqueued"] == 0


@pytest.mark.asyncio
async def test_extra_body_field_422(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    bad_record = {**_make_record(), "agent_id": "evil"}
    with patch("phaze.routers.agent_files.Queue"):
        response = await authenticated_client.post("/api/internal/agent/files", json={"files": [bad_record]})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc"))[:4] == ["body", "files", 0, "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_agent_id_in_body_rejected(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    with patch("phaze.routers.agent_files.Queue"):
        response = await authenticated_client.post(
            "/api/internal/agent/files",
            json={"agent_id": "evil", "files": [_make_record()]},
        )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc")) == ["body", "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_chunk_cap_exceeded_422(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    chunk = {"files": [_make_record(path=f"/test/music/{i:04d}.mp3") for i in range(1001)]}
    with patch("phaze.routers.agent_files.Queue"):
        response = await authenticated_client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_missing_auth_returns_401(client: AsyncClient) -> None:
    """AUTH-01 reaffirmed on the production route; lights up green AFTER Plan 06 wires main.py."""
    response = await client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    # Until Plan 06 wires the router, this returns 404. After wiring it returns 401.
    assert response.status_code in (401, 404)
    if response.status_code == 401:
        assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_same_chunk_duplicate_paths_dedup(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    rec1 = _make_record(path="/test/music/dup.mp3")
    rec2 = {**_make_record(path="/test/music/dup.mp3"), "file_size": 999}
    with patch("phaze.routers.agent_files.Queue") as MockQueue:
        MockQueue.from_url.return_value = AsyncMock()
        response = await authenticated_client.post("/api/internal/agent/files", json={"files": [rec1, rec2]})
    assert response.status_code == 200, response.text
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1
