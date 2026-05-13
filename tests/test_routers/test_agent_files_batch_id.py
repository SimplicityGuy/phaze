"""Contract tests for the new `batch_id` field on POST /api/internal/agent/files (Phase 27 D-09, D-18, D-21).

The Phase 25 upsert endpoint now accepts an optional `batch_id`:
- present  -> SELECT the batch by id; 404 if missing; 403 if `batch.agent_id != caller.id`
              (cross-tenant guard BEFORE the records loop, T-27-02). Bind all files in the
              chunk to that batch.
- absent   -> SELECT the calling agent's LIVE sentinel batch
              (`WHERE agent_id=? AND status='live'`; the partial unique index
              `uq_scan_batches_agent_id_live` guarantees ≤1 row). Bind all files to it.

The existing auto-enqueue path (xmax-based INSERT detection) is unaffected --
the Phase 26 SCAN-02 invariant requires `extract_file_metadata` to fire for
freshly-INSERTed music/video files regardless of which batch they bind to.

This file's smoke-app fixture mirrors `tests/test_routers/test_agent_files.py:52-96`
verbatim so the production handler is exercised under a minimal app.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import func as sa_func, select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import agent_files


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> tuple[FastAPI, AsyncMock]:
    """Build a FastAPI app wiring agent_files.router with a mocked task_router (Phase 26-12 pattern)."""
    app = FastAPI(title="agent-files-batch-smoke", version="test")
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
    """Smoke-app fixture exposing both the test client AND the mock task_router."""
    _agent, raw_token = seed_test_agent
    app, mock_router = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        yield ac, mock_router


def _make_record(path: str = "/test/music/a.mp3", ext: str = "mp3", size: int = 100) -> dict[str, object]:
    return {
        "sha256_hash": "0" * 64,
        "original_path": path,
        "original_filename": path.rsplit("/", 1)[-1],
        "current_path": path,
        "file_type": ext,
        "file_size": size,
    }


async def _seed_batch(
    session: AsyncSession,
    agent_id: str,
    status: ScanStatus = ScanStatus.RUNNING,
    scan_path: str = "/test/music",
) -> uuid.UUID:
    """Seed a ScanBatch and return its id."""
    batch_id = uuid.uuid4()
    batch = ScanBatch(
        id=batch_id,
        agent_id=agent_id,
        scan_path=scan_path,
        status=status.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()
    return batch_id


async def _seed_live_sentinel(session: AsyncSession, agent_id: str) -> uuid.UUID:
    """Seed the LIVE sentinel batch for the given agent (Phase 24 D-09..D-12)."""
    return await _seed_batch(session, agent_id, status=ScanStatus.LIVE, scan_path="<watcher>")


@pytest.mark.asyncio
async def test_batch_id_present_binds_files_to_that_batch(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-09 + D-21: explicit batch_id binds the chunk's files to that batch."""
    client, _ = smoke_app_and_router
    agent, _ = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)

    chunk = {"batch_id": str(batch_id), "files": [_make_record(path="/test/music/a.mp3")]}
    r = await client.post("/api/internal/agent/files", json=chunk)
    assert r.status_code == 200, r.text

    # Verify the FileRecord row was bound to the explicit batch.
    await session.commit()
    session.expire_all()
    row = (await session.execute(select(FileRecord).where(FileRecord.original_path == "/test/music/a.mp3"))).scalar_one()
    assert row.batch_id == batch_id


@pytest.mark.asyncio
async def test_batch_id_absent_resolves_live_sentinel(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """D-18: batch_id omitted -> server resolves the agent's LIVE sentinel batch and binds to it."""
    client, _ = smoke_app_and_router
    agent, _ = seed_test_agent
    live_batch_id = await _seed_live_sentinel(session, agent.id)

    chunk = {"files": [_make_record(path="/test/music/live.mp3")]}
    r = await client.post("/api/internal/agent/files", json=chunk)
    assert r.status_code == 200, r.text

    await session.commit()
    session.expire_all()
    row = (await session.execute(select(FileRecord).where(FileRecord.original_path == "/test/music/live.mp3"))).scalar_one()
    assert row.batch_id == live_batch_id


@pytest.mark.asyncio
async def test_batch_id_cross_agent_403(
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """T-27-02: agent B POSTing with agent A's batch_id -> 403, ZERO rows inserted."""
    agent_a, _ = seed_test_agent
    batch_id = await _seed_batch(session, agent_a.id, ScanStatus.RUNNING)
    # Even though agent A has a LIVE sentinel, the present-batch_id branch
    # should reject BEFORE evaluating sentinel resolution. Seed it for realism.
    await _seed_live_sentinel(session, agent_a.id)

    # Seed agent B inline (mirror test_agent_proposals.py:208-217).
    raw_token_b = "phaze_agent_" + secrets.token_urlsafe(32)
    token_hash_b = hashlib.sha256(raw_token_b.encode("utf-8")).hexdigest()
    agent_b = Agent(
        id="test-agent-b",
        name="test-agent-b",
        token_hash=token_hash_b,
        scan_roots=["/test/b"],
    )
    session.add(agent_b)
    await session.commit()
    # Agent B also has its own LIVE sentinel (so the absent branch wouldn't 500).
    await _seed_live_sentinel(session, agent_b.id)

    app, _mock_router = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token_b}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        chunk = {"batch_id": str(batch_id), "files": [_make_record(path="/test/music/cross.mp3")]}
        r = await ac.post("/api/internal/agent/files", json=chunk)

    assert r.status_code == 403, f"Expected 403 (cross-tenant), got {r.status_code}: {r.text}"
    assert "does not belong" in r.text.lower() or "belong to authenticated" in r.text.lower()

    # Atomicity: NO FileRecord rows were inserted.
    await session.commit()
    session.expire_all()
    count = (await session.execute(select(sa_func.count()).select_from(FileRecord))).scalar_one()
    assert count == 0, "cross-tenant 403 must abort BEFORE any FileRecord insert"


@pytest.mark.asyncio
async def test_batch_id_unknown_404(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Unknown batch_id -> 404 'scan batch not found'."""
    client, _ = smoke_app_and_router
    _agent, _ = seed_test_agent
    unknown_id = uuid.uuid4()
    chunk = {"batch_id": str(unknown_id), "files": [_make_record(path="/test/music/x.mp3")]}
    r = await client.post("/api/internal/agent/files", json=chunk)
    assert r.status_code == 404, r.text
    assert "not found" in r.text.lower()

    # Atomicity: no rows inserted on a 404 path either.
    await session.commit()
    session.expire_all()
    count = (await session.execute(select(sa_func.count()).select_from(FileRecord))).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_auto_enqueue_with_explicit_batch_id(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """SCAN-02: auto-enqueue STILL fires for new INSERTs when batch_id is explicit."""
    client, mock_router = smoke_app_and_router
    agent, _ = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)

    chunk = {"batch_id": str(batch_id), "files": [_make_record(path="/test/music/enq.mp3")]}
    r = await client.post("/api/internal/agent/files", json=chunk)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1
    assert body["enqueued"] == 1

    # The auto-enqueue path called task_router.enqueue_for_agent exactly once,
    # with task_name="extract_file_metadata".
    assert mock_router.enqueue_for_agent.await_count == 1
    call = mock_router.enqueue_for_agent.await_args_list[0]
    assert call.kwargs["task_name"] == "extract_file_metadata"
    assert call.kwargs["agent_id"] == agent.id
