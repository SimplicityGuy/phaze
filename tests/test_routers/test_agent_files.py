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

Plan 26-12 update:
    Handler refactor swapped the inline ``Queue.from_url(...)`` for the
    lifespan-wired ``app.state.task_router`` (an ``AgentTaskRouter``). The
    smoke-app fixture now installs an ``AsyncMock()`` at
    ``app.state.task_router`` so the handler can call
    ``await request.app.state.task_router.enqueue_for_agent(...)`` without
    needing a real Redis connection. Assertions migrate from
    ``mock_queue.enqueue.await_args_list`` to
    ``mock_router.enqueue_for_agent.await_args_list``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import func as sa_func, select

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.routers import agent_files


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


def _make_smoke_app(session: AsyncSession) -> tuple[FastAPI, AsyncMock]:
    """Build a FastAPI app wiring agent_files.router so Wave-3 tests can call the real handler.

    Returns the app AND the AsyncMock installed at ``app.state.task_router`` so the
    test can introspect enqueue calls (Plan 26-12 refactor: handler now reads from
    ``request.app.state.task_router`` instead of constructing a Queue inline).
    """
    app = FastAPI(title="agent-files-smoke", version="test")
    app.include_router(agent_files.router)
    app.dependency_overrides[get_session] = lambda: session
    mock_router = AsyncMock()
    app.state.task_router = mock_router
    return app, mock_router


@pytest_asyncio.fixture
async def smoke_app_and_router(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> AsyncGenerator[tuple[AsyncClient, AsyncMock]]:
    """Smoke-app fixture exposing both the test client AND the mock task_router.

    Tests that need to assert against enqueue calls (e.g.,
    ``test_auto_enqueue_only_for_inserts``) consume this fixture; tests that
    only care about the HTTP response can use ``authenticated_client`` below,
    which is a thin wrapper that drops the router handle.
    """
    _agent, raw_token = seed_test_agent
    app, mock_router = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac, mock_router


@pytest_asyncio.fixture
async def authenticated_client(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
) -> AsyncGenerator[AsyncClient]:
    """LOCAL OVERRIDE of conftest.authenticated_client: drops the router handle for tests that don't need it.

    Replaces the conftest version (which uses ``create_app()`` and therefore lacks
    the agent_files router until Plan 06). Same Authorization header convention.
    """
    client, _ = smoke_app_and_router
    yield client


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
    r1 = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    r2 = await authenticated_client.post("/api/internal/agent/files", json={"files": [_make_record()]})
    assert r1.status_code == 200
    assert r2.status_code == 200
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_auto_enqueue_only_for_inserts(smoke_app_and_router: tuple[AsyncClient, AsyncMock], seed_test_agent: tuple[Agent, str]) -> None:
    """D-21/D-22: handler delegates to ``app.state.task_router.enqueue_for_agent`` per INSERTed music/video row."""
    client, mock_router = smoke_app_and_router
    agent, _ = seed_test_agent
    chunk = {"files": [_make_record(path="/test/music/a.mp3"), _make_record(path="/test/music/b.mp3")]}
    response = await client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 200
    assert mock_router.enqueue_for_agent.await_count == 2
    for call in mock_router.enqueue_for_agent.await_args_list:
        kwargs = call.kwargs
        assert kwargs["agent_id"] == agent.id
        assert kwargs["task_name"] == "extract_file_metadata"
        payload = kwargs["payload"]
        # payload is an ExtractMetadataPayload; introspect via attribute access
        assert isinstance(payload.file_id, uuid.UUID)
        assert payload.file_type == "mp3"
        assert payload.agent_id == agent.id
        assert payload.original_path.startswith("/test/music/")


@pytest.mark.asyncio
async def test_no_enqueue_for_updates(smoke_app_and_router: tuple[AsyncClient, AsyncMock], seed_test_agent: tuple[Agent, str]) -> None:
    client, mock_router = smoke_app_and_router
    chunk = {"files": [_make_record()]}
    r1 = await client.post("/api/internal/agent/files", json=chunk)
    assert r1.status_code == 200
    assert mock_router.enqueue_for_agent.await_count == 1
    mock_router.enqueue_for_agent.reset_mock()
    r2 = await client.post("/api/internal/agent/files", json=chunk)
    assert r2.status_code == 200
    assert mock_router.enqueue_for_agent.await_count == 0
    body = r2.json()
    assert body["inserted"] == 0
    assert body["upserted"] == 1
    assert body["enqueued"] == 0


@pytest.mark.asyncio
async def test_extra_body_field_422(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
    bad_record = {**_make_record(), "agent_id": "evil"}
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [bad_record]})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e.get("type") == "extra_forbidden" and list(e.get("loc"))[:4] == ["body", "files", 0, "agent_id"] for e in errors), errors


@pytest.mark.asyncio
async def test_agent_id_in_body_rejected(authenticated_client: AsyncClient, seed_test_agent: tuple[Agent, str]) -> None:
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
    response = await authenticated_client.post("/api/internal/agent/files", json={"files": [rec1, rec2]})
    assert response.status_code == 200, response.text
    result = await session.execute(select(sa_func.count()).select_from(FileRecord))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_no_enqueue_for_non_music_file_type(smoke_app_and_router: tuple[AsyncClient, AsyncMock], seed_test_agent: tuple[Agent, str]) -> None:
    """Non-music/video file types (e.g., .txt, .jpg) must skip the enqueue path even on INSERT."""
    client, mock_router = smoke_app_and_router
    chunk = {
        "files": [
            _make_record(path="/test/docs/readme.txt", ext="txt"),
            _make_record(path="/test/docs/cover.jpg", ext="jpg"),
        ],
    }
    response = await client.post("/api/internal/agent/files", json=chunk)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inserted"] == 2
    assert body["enqueued"] == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_exception_does_not_abort_response(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock], seed_test_agent: tuple[Agent, str]
) -> None:
    """Enqueue failure must be swallowed + counted as `enqueued=0` -- DB commit already succeeded."""
    client, mock_router = smoke_app_and_router
    mock_router.enqueue_for_agent.side_effect = RuntimeError("redis is sick")

    chunk = {"files": [_make_record(path="/test/music/a.mp3")]}
    response = await client.post("/api/internal/agent/files", json=chunk)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["inserted"] == 1
    assert body["enqueued"] == 0
    # Handler attempted the enqueue (got to the side_effect) before catching + continuing.
    mock_router.enqueue_for_agent.assert_awaited_once()
