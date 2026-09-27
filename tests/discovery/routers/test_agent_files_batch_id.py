"""Contract tests for the new `batch_id` field on POST /api/internal/agent/files (Phase 27 D-09, D-18, D-21).

The Phase 25 upsert endpoint now accepts an optional `batch_id`:
- present  -> SELECT the batch by id; 404 if missing; 403 if `batch.agent_id != caller.id`
              (cross-tenant guard BEFORE the records loop, T-27-02). Bind all files in the
              chunk to that batch.
- absent   -> SELECT the calling agent's LIVE sentinel batch
              (`WHERE agent_id=? AND status='live'`; the partial unique index
              `uq_scan_batches_agent_id_live` guarantees ≤1 row). Bind all files to it.

Phase 35 (D-06): the former per-INSERT auto-enqueue has been removed. Discovery binds
files to a batch and persists them, but never auto-enqueues the metadata-extraction task
(metadata extraction is operator-triggered only) -- regardless of which batch they bind to.

This file's smoke-app fixture mirrors `tests/test_routers/test_agent_files.py:52-96`
verbatim so the production handler is exercised under a minimal app.
"""

from __future__ import annotations

import hashlib
import logging
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
async def test_no_auto_enqueue_with_explicit_batch_id(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Phase 35 (D-06): a new INSERT bound to an explicit batch_id does NOT auto-enqueue."""
    client, mock_router = smoke_app_and_router
    agent, _ = seed_test_agent
    batch_id = await _seed_batch(session, agent.id, ScanStatus.RUNNING)

    chunk = {"batch_id": str(batch_id), "files": [_make_record(path="/test/music/enq.mp3")]}
    r = await client.post("/api/internal/agent/files", json=chunk)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1
    assert body["enqueued"] == 0

    # No enqueue regardless of batch binding -- metadata extraction is operator-triggered.
    mock_router.enqueue_for_agent.assert_not_awaited()


# phaze-tvdu1: regression tests for the self-healing LIVE-sentinel fix.
#
# Unlike `tests/discovery/routers/test_agent_files.py`'s `smoke_app_and_router` fixture, THIS
# file's `smoke_app_and_router` (above) does NOT pre-seed a LIVE sentinel for `seed_test_agent` --
# it only seeds the agent itself. That is exactly the "agent registered with no sentinel" shape
# the bug reproduces: pre-fix, a `batch_id`-omitted POST against a freshly seeded agent raised
# `sqlalchemy.exc.NoResultFound` at the handler's `.scalar_one()` LIVE-batch lookup, which FastAPI
# surfaces as an uncaught 500 -- and the watcher drops the file chunk. Post-fix, the handler
# self-heals via `phaze.services.live_sentinel.ensure_live_sentinel` instead of crashing.


@pytest.mark.asyncio
async def test_batch_id_absent_self_heals_missing_sentinel(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-tvdu1: an agent with NO LIVE sentinel gets one created on demand, not a 500."""
    client, _ = smoke_app_and_router
    agent, _ = seed_test_agent
    agent_id = agent.id  # captured before `expire_all()` below -- see phaze-30ssq-style note

    # Precondition: this agent truly has no LIVE sentinel yet.
    pre_existing = (
        await session.execute(select(sa_func.count()).select_from(ScanBatch).where(ScanBatch.agent_id == agent_id, ScanBatch.status == "live"))
    ).scalar_one()
    assert pre_existing == 0, "test precondition: agent must start with no LIVE sentinel"

    chunk = {"files": [_make_record(path="/test/music/self-heal.mp3")]}
    r = await client.post("/api/internal/agent/files", json=chunk)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1
    assert body["upserted"] == 1

    await session.commit()
    session.expire_all()

    live_batches = (
        (await session.execute(select(ScanBatch).where(ScanBatch.agent_id == agent_id, ScanBatch.status == ScanStatus.LIVE.value))).scalars().all()
    )
    assert len(live_batches) == 1, f"expected exactly one self-healed LIVE sentinel, got {len(live_batches)}"
    assert live_batches[0].scan_path == "<watcher>"

    row = (await session.execute(select(FileRecord).where(FileRecord.original_path == "/test/music/self-heal.mp3"))).scalar_one()
    assert row.batch_id == live_batches[0].id


@pytest.mark.asyncio
async def test_batch_id_absent_self_heal_logs_warning(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """phaze-tvdu1: the self-heal path is observable -- a WARNING, never a silent 500."""
    client, _ = smoke_app_and_router
    agent, _ = seed_test_agent

    chunk = {"files": [_make_record(path="/test/music/self-heal-warn.mp3")]}
    with caplog.at_level(logging.WARNING, logger="phaze.routers.agent_files"):
        r = await client.post("/api/internal/agent/files", json=chunk)
    assert r.status_code == 200, r.text

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "no LIVE sentinel" in text
    assert agent.id in text


@pytest.mark.asyncio
async def test_batch_id_absent_second_call_reuses_self_healed_sentinel(
    smoke_app_and_router: tuple[AsyncClient, AsyncMock],
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """phaze-tvdu1: the second batch_id-omitted call reuses the sentinel the first call created."""
    client, _ = smoke_app_and_router
    agent, _ = seed_test_agent
    agent_id = agent.id  # captured before `expire_all()` below

    r1 = await client.post("/api/internal/agent/files", json={"files": [_make_record(path="/test/music/first.mp3")]})
    assert r1.status_code == 200, r1.text
    r2 = await client.post("/api/internal/agent/files", json={"files": [_make_record(path="/test/music/second.mp3")]})
    assert r2.status_code == 200, r2.text

    await session.commit()
    session.expire_all()

    live_batches = (
        (await session.execute(select(ScanBatch).where(ScanBatch.agent_id == agent_id, ScanBatch.status == ScanStatus.LIVE.value))).scalars().all()
    )
    assert len(live_batches) == 1, f"expected the second call to reuse the first-created sentinel, got {len(live_batches)}"

    rows = (
        (await session.execute(select(FileRecord.batch_id).where(FileRecord.original_path.in_(["/test/music/first.mp3", "/test/music/second.mp3"]))))
        .scalars()
        .all()
    )
    assert set(rows) == {live_batches[0].id}, "both upserts must bind to the SAME self-healed sentinel"
