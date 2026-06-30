"""Integration tests for POST /execution/start dispatch rewrite + SSE extension (Phase 28 D-09, D-11).

Targets:
- 28-V-04 — :func:`test_multi_agent_dispatch_enqueues_per_chunk`
- 28-V-05 — :func:`test_dispatch_summary_in_redis_hash`
- 28-V-18 — :func:`test_sse_emits_aggregate_progress`
- 28-V-19 — :func:`test_sse_emits_agents_table`
- 28-V-20 — :func:`test_sse_closes_on_complete_with_errors`

Tests use real PostgreSQL (via the project's ``session`` fixture) and real
Redis (via a local ``redis_client`` fixture).
``app.state.task_router.enqueue_for_agent`` is mocked with ``AsyncMock`` since
spinning up a real SAQ worker per test is too heavy. ``app.state.redis`` uses
the real Redis client so HSET / HGETALL / HEXISTS round-trip the data the
dispatch path commits.

Worktree-isolation note (Plan 28-04): runs in parallel with Plan 28-05. The
two pytest processes share the host-level Postgres container. To prevent the
shared-DB race seen in 28-02/28-03, honour ``PHAZE_TEST_DATABASE_URL_28_04``
if set — the orchestrator points it at a worktree-dedicated database.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers import execution


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")
_OVERRIDE_DB_URL = os.environ.get("PHAZE_TEST_DATABASE_URL_28_04")


@pytest.fixture(autouse=True)
def _override_test_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``tests.conftest.TEST_DATABASE_URL`` at a worktree-dedicated DB if set."""
    if _OVERRIDE_DB_URL:
        import tests.conftest as _conftest

        monkeypatch.setattr(_conftest, "TEST_DATABASE_URL", _OVERRIDE_DB_URL)


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[redis_async.Redis]:
    """Real Redis client with decode_responses=True (matches production wiring).

    Cleans up ``exec:*`` keys around each test so reruns do not collide.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL, decode_responses=True)
    for pattern in ("exec:*", "exec_progress_req:*"):
        keys = [k async for k in client.scan_iter(match=pattern, count=100)]
        if keys:
            await client.delete(*keys)
    try:
        yield client
    finally:
        for pattern in ("exec:*", "exec_progress_req:*"):
            keys = [k async for k in client.scan_iter(match=pattern, count=100)]
            if keys:
                await client.delete(*keys)
        await client.aclose()


def _make_smoke_app(
    session: AsyncSession,
    redis_client: redis_async.Redis,
) -> tuple[FastAPI, AsyncMock]:
    """Build a smoke FastAPI app mounting the execution router.

    Returns the app AND the AsyncMock at ``app.state.task_router`` so
    happy-path tests can assert against ``enqueue_for_agent`` call args.
    """
    app = FastAPI(title="execution-dispatch-smoke", version="test")
    app.include_router(execution.router)
    app.dependency_overrides[get_session] = lambda: session
    mock_router = AsyncMock()
    app.state.task_router = mock_router
    app.state.redis = redis_client
    # Defensive: routers occasionally reach for app.state.queue (legacy code paths).
    app.state.queue = AsyncMock()
    return app, mock_router


@pytest_asyncio.fixture
async def smoke(
    session: AsyncSession,
    redis_client: redis_async.Redis,
) -> AsyncGenerator[tuple[AsyncClient, AsyncMock, redis_async.Redis]]:
    """Smoke client + mock task_router + redis_client; no seed (tests seed inline)."""
    app, mock_router = _make_smoke_app(session, redis_client)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, mock_router, redis_client


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_agent(session: AsyncSession, *, agent_id: str, name: str | None = None, revoked: bool = False) -> Agent:
    from datetime import UTC, datetime

    agent = Agent(
        id=agent_id,
        name=name or agent_id,
        token_hash=None,
        scan_roots=[],
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _seed_approved_proposal(
    session: AsyncSession,
    *,
    agent_id: str,
    path_suffix: str,
    status: ProposalStatus = ProposalStatus.APPROVED,
) -> RenameProposal:
    """Insert a (FileRecord, RenameProposal) pair owned by agent_id, approved by default."""
    file_id = uuid.uuid4()
    fr = FileRecord(
        id=file_id,
        sha256_hash=(uuid.uuid4().hex + uuid.uuid4().hex),
        original_path=f"/music/{agent_id}/{path_suffix}.mp3",
        original_filename=f"{path_suffix}.mp3",
        current_path=f"/music/{agent_id}/{path_suffix}.mp3",
        file_type="music",
        file_size=1_000_000,
        state=FileState.APPROVED,
        agent_id=agent_id,
    )
    session.add(fr)
    await session.flush()
    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename=f"new-{path_suffix}.mp3",
        proposed_path=f"/output/{agent_id}/{path_suffix}",
        confidence=0.9,
        status=status,
    )
    session.add(proposal)
    await session.commit()
    await session.refresh(proposal)
    return proposal


# ---------------------------------------------------------------------------
# 28-V-04: multi-agent dispatch enqueues one sub-job per (agent, chunk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_agent_dispatch_enqueues_per_chunk(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    session: AsyncSession,
) -> None:
    """3 agents x varying proposals -> 1 + 2 + 1 = 4 enqueue calls, sub_batch_index assigned."""
    ac, mock_router, _redis = smoke
    # Use small counts to keep the test fast but still cross the 500 chunk boundary.
    # Agent A: 1 chunk (100 items). Agent B: 2 chunks (600 items, 500 + 100). Agent C: 1 chunk (250 items).
    # Use ONLY agent B with 600 items + agent A with 100 + agent C with 250 for distinct chunk counts.
    await _seed_agent(session, agent_id="agent-a")
    await _seed_agent(session, agent_id="agent-b")
    await _seed_agent(session, agent_id="agent-c")
    for i in range(100):
        await _seed_approved_proposal(session, agent_id="agent-a", path_suffix=f"a-{i:04d}")
    for i in range(600):
        await _seed_approved_proposal(session, agent_id="agent-b", path_suffix=f"b-{i:04d}")
    for i in range(250):
        await _seed_approved_proposal(session, agent_id="agent-c", path_suffix=f"c-{i:04d}")

    response = await ac.post("/execution/start")
    assert response.status_code == 200, response.text

    # 4 sub-jobs total: 1 (agent-a) + 2 (agent-b) + 1 (agent-c)
    assert mock_router.enqueue_for_agent.await_count == 4

    # Verify the per-call structure: each call gets a chunk-of-<=500 payload
    by_agent: dict[str, list[int]] = {}
    for call in mock_router.enqueue_for_agent.await_args_list:
        kwargs = call.kwargs
        assert kwargs["task_name"] == "execute_approved_batch"
        payload = kwargs["payload"]
        agent_id = kwargs["agent_id"]
        # ExecuteApprovedBatchPayload.agent_id matches the per-(agent, chunk) routing key
        assert payload.agent_id == agent_id
        # batch_id is the same UUID across all sub-jobs
        assert isinstance(payload.batch_id, uuid.UUID)
        by_agent.setdefault(agent_id, []).append(payload.sub_batch_index)
        assert 1 <= len(payload.proposals) <= 500

    # Sub-batch index 0 must always be present; agent-b also has sub_batch_index 1
    assert sorted(by_agent["agent-a"]) == [0]
    assert sorted(by_agent["agent-b"]) == [0, 1]
    assert sorted(by_agent["agent-c"]) == [0]


# ---------------------------------------------------------------------------
# 28-V-05: dispatch_summary visible in exec:{batch_id} Redis hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_summary_in_redis_hash(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    session: AsyncSession,
) -> None:
    """POST /execution/start seeds the D-04 hash fields including dispatch_summary JSON."""
    ac, _mock_router, redis_client = smoke
    await _seed_agent(session, agent_id="agent-a", name="Agent Alpha")
    await _seed_agent(session, agent_id="agent-b", name="Agent Beta")
    for i in range(5):
        await _seed_approved_proposal(session, agent_id="agent-a", path_suffix=f"a-{i}")
    for i in range(7):
        await _seed_approved_proposal(session, agent_id="agent-b", path_suffix=f"b-{i}")

    response = await ac.post("/execution/start")
    assert response.status_code == 200, response.text

    # Find the exec:{batch_id} key the dispatch wrote.
    exec_keys = [k async for k in redis_client.scan_iter(match="exec:*", count=100)]
    assert len(exec_keys) == 1, f"expected exactly one exec:* key, found {exec_keys}"
    key = exec_keys[0]
    data = await redis_client.hgetall(key)

    # D-04 schema verification
    assert int(data["total"]) == 12
    assert int(data["subjobs_expected"]) == 2  # one chunk per agent
    assert int(data["subjobs_completed"]) == 0
    assert int(data["completed"]) == 0
    assert int(data["failed"]) == 0
    assert int(data["copied"]) == 0
    assert int(data["verified"]) == 0
    assert int(data["deleted"]) == 0
    assert data["status"] == "running"
    assert "started_at" in data
    # Per-agent rollups pre-seeded so D-17 step 4 HEXISTS check succeeds
    assert int(data["agent:agent-a:total"]) == 5
    assert int(data["agent:agent-a:completed"]) == 0
    assert int(data["agent:agent-a:failed"]) == 0
    assert int(data["agent:agent-b:total"]) == 7
    assert int(data["agent:agent-b:completed"]) == 0
    assert int(data["agent:agent-b:failed"]) == 0

    # dispatch_summary is JSON-parseable to a list with both agent keys.
    summary = json.loads(data["dispatch_summary"])
    assert isinstance(summary, list)
    assert len(summary) == 2
    by_id = {item["agent_id"]: item for item in summary}
    assert by_id["agent-a"]["total"] == 5
    assert by_id["agent-a"]["chunks"] == 1
    assert by_id["agent-b"]["total"] == 7
    assert by_id["agent-b"]["chunks"] == 1

    # 24h TTL set atomically with HSET.
    ttl = await redis_client.ttl(key)
    assert 86000 < ttl <= 86400, f"expected ~24h TTL, got {ttl}"


# ---------------------------------------------------------------------------
# Dispatch INFO log line (D-11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_logs_info_line(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-11: dispatch emits INFO 'dispatch batch_id=... total=N n_agents=M subjobs_expected=K'."""
    ac, _mock_router, _redis = smoke
    await _seed_agent(session, agent_id="agent-only")
    for i in range(3):
        await _seed_approved_proposal(session, agent_id="agent-only", path_suffix=f"x-{i}")

    with caplog.at_level(logging.INFO, logger="phaze.routers.execution"):
        response = await ac.post("/execution/start")
    assert response.status_code == 200

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "dispatch batch_id=" in messages
    assert "total=3" in messages
    assert "n_agents=1" in messages
    assert "subjobs_expected=1" in messages


# ---------------------------------------------------------------------------
# Revoked-agents banner surfaces when skipped_revoked > 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_agent_renders_banner(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    session: AsyncSession,
) -> None:
    """An approved proposal on a revoked agent -> orange-surface banner in the response."""
    ac, mock_router, _redis = smoke
    await _seed_agent(session, agent_id="agent-ok")
    await _seed_agent(session, agent_id="agent-revoked", revoked=True)
    await _seed_approved_proposal(session, agent_id="agent-ok", path_suffix="ok-1")
    await _seed_approved_proposal(session, agent_id="agent-revoked", path_suffix="rev-1")

    response = await ac.post("/execution/start")
    assert response.status_code == 200, response.text
    assert "Some proposals skipped" in response.text
    assert "bg-orange-50" in response.text
    # The non-revoked agent still gets enqueued.
    assert mock_router.enqueue_for_agent.await_count == 1
    payload = mock_router.enqueue_for_agent.await_args_list[0].kwargs["payload"]
    assert payload.agent_id == "agent-ok"


# ---------------------------------------------------------------------------
# Collision short-circuits dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collision_short_circuits_dispatch(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    session: AsyncSession,
) -> None:
    """Two approved proposals targeting the same destination -> collision_block, no Redis seed, no enqueue."""
    ac, mock_router, redis_client = smoke
    await _seed_agent(session, agent_id="agent-a")
    # Two proposals with the SAME (proposed_path, proposed_filename) -> collision.
    fr1_id = uuid.uuid4()
    fr2_id = uuid.uuid4()
    fr1 = FileRecord(
        id=fr1_id,
        sha256_hash=(uuid.uuid4().hex + uuid.uuid4().hex),
        original_path="/music/agent-a/coll-1.mp3",
        original_filename="coll-1.mp3",
        current_path="/music/agent-a/coll-1.mp3",
        file_type="music",
        file_size=1_000_000,
        state=FileState.APPROVED,
        agent_id="agent-a",
    )
    fr2 = FileRecord(
        id=fr2_id,
        sha256_hash=(uuid.uuid4().hex + uuid.uuid4().hex),
        original_path="/music/agent-a/coll-2.mp3",
        original_filename="coll-2.mp3",
        current_path="/music/agent-a/coll-2.mp3",
        file_type="music",
        file_size=1_000_000,
        state=FileState.APPROVED,
        agent_id="agent-a",
    )
    session.add_all([fr1, fr2])
    await session.flush()
    session.add_all(
        [
            RenameProposal(
                id=uuid.uuid4(),
                file_id=fr1_id,
                proposed_filename="duplicate.mp3",
                proposed_path="/output/coll",
                status=ProposalStatus.APPROVED,
            ),
            RenameProposal(
                id=uuid.uuid4(),
                file_id=fr2_id,
                proposed_filename="duplicate.mp3",
                proposed_path="/output/coll",
                status=ProposalStatus.APPROVED,
            ),
        ]
    )
    await session.commit()

    response = await ac.post("/execution/start")
    assert response.status_code == 200
    # Collision-block content (not the progress card).
    assert "Path collisions detected" in response.text
    # NO Redis writes.
    exec_keys = [k async for k in redis_client.scan_iter(match="exec:*", count=100)]
    assert exec_keys == []
    # NO enqueues.
    mock_router.enqueue_for_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# SSE generator behavior
# ---------------------------------------------------------------------------


async def _consume_sse(generator, max_events: int) -> list[dict[str, str]]:
    """Consume at most ``max_events`` items from an async SSE generator.

    Returns a list of ``{"event": str, "data": str}`` dicts. Stops when the
    generator returns (StopAsyncIteration) or when ``max_events`` is reached.
    """
    events: list[dict[str, str]] = []
    try:
        async for event in generator:
            events.append(event)
            if len(events) >= max_events:
                break
    except StopAsyncIteration:
        pass
    return events


@pytest.mark.asyncio
async def test_sse_emits_aggregate_progress(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """28-V-18: SSE generator yields a 'progress' event with the aggregate counter HTML."""
    _ac, _mock_router, redis_client = smoke
    # Speed the generator up so we can consume a few ticks fast.
    monkeypatch.setattr("phaze.routers.execution.asyncio.sleep", AsyncMock(return_value=None))

    batch_id = uuid.uuid4()
    # Pre-seed a non-terminal hash so the generator emits progress + agents_table on first tick.
    from datetime import UTC, datetime

    await redis_client.hset(
        f"exec:{batch_id}",
        mapping={
            "total": 10,
            "completed": 3,
            "failed": 0,
            "copied": 3,
            "verified": 3,
            "deleted": 3,
            "subjobs_completed": 0,
            "subjobs_expected": 1,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "agent:agent-a:total": 10,
            "agent:agent-a:completed": 3,
            "agent:agent-a:failed": 0,
            "dispatch_summary": json.dumps([{"agent_id": "agent-a", "name": "Alpha", "total": 10, "chunks": 1}]),
        },
    )

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/execution/progress/{batch_id}",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": _build_app_stub(redis_client),
    }
    request = Request(scope=scope)  # type: ignore[arg-type]
    response = await execution.execution_progress(request, str(batch_id))
    events = await _consume_sse(response.body_iterator, max_events=5)
    event_names = [e.get("event") for e in events]
    assert "progress" in event_names
    progress = next(e for e in events if e.get("event") == "progress")
    # The data contains the aggregate counter values (rendered HTML).
    assert "3" in progress["data"]  # completed
    assert "10" in progress["data"]  # total


@pytest.mark.asyncio
async def test_sse_emits_agents_table(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """28-V-19: SSE generator yields an 'agents_table' event with rendered HTML rows."""
    _ac, _mock_router, redis_client = smoke
    monkeypatch.setattr("phaze.routers.execution.asyncio.sleep", AsyncMock(return_value=None))

    batch_id = uuid.uuid4()
    from datetime import UTC, datetime

    await redis_client.hset(
        f"exec:{batch_id}",
        mapping={
            "total": 10,
            "completed": 3,
            "failed": 0,
            "copied": 3,
            "verified": 3,
            "deleted": 3,
            "subjobs_completed": 0,
            "subjobs_expected": 1,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "agent:agent-a:total": 10,
            "agent:agent-a:completed": 3,
            "agent:agent-a:failed": 0,
            "dispatch_summary": json.dumps([{"agent_id": "agent-a", "name": "Alpha", "total": 10, "chunks": 1}]),
        },
    )

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/execution/progress/{batch_id}",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": _build_app_stub(redis_client),
    }
    request = Request(scope=scope)  # type: ignore[arg-type]
    response = await execution.execution_progress(request, str(batch_id))
    events = await _consume_sse(response.body_iterator, max_events=5)

    agents_events = [e for e in events if e.get("event") == "agents_table"]
    assert agents_events, f"expected agents_table event, got events={[e.get('event') for e in events]}"
    # Rendered HTML carries the agent row + the RUNNING pill.
    html = agents_events[0]["data"]
    assert "agent-a" in html
    assert "RUNNING" in html


@pytest.mark.asyncio
async def test_sse_emits_dispatch_summary_on_first_connect_only(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatch_summary event yielded ONCE; subsequent ticks must NOT re-emit it."""
    _ac, _mock_router, redis_client = smoke
    monkeypatch.setattr("phaze.routers.execution.asyncio.sleep", AsyncMock(return_value=None))

    batch_id = uuid.uuid4()
    from datetime import UTC, datetime

    await redis_client.hset(
        f"exec:{batch_id}",
        mapping={
            "total": 10,
            "completed": 3,
            "failed": 0,
            "copied": 3,
            "verified": 3,
            "deleted": 3,
            "subjobs_completed": 0,
            "subjobs_expected": 1,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "agent:agent-a:total": 10,
            "agent:agent-a:completed": 3,
            "agent:agent-a:failed": 0,
            "dispatch_summary": json.dumps([{"agent_id": "agent-a", "name": "Alpha", "total": 10, "chunks": 1}]),
        },
    )

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/execution/progress/{batch_id}",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": _build_app_stub(redis_client),
    }
    request = Request(scope=scope)  # type: ignore[arg-type]
    response = await execution.execution_progress(request, str(batch_id))
    # Drain several ticks. With sleep mocked + non-terminal status, the generator
    # never closes on its own; cap at 12 events ~= 4 ticks * 3 events each.
    events = await _consume_sse(response.body_iterator, max_events=12)
    summary_events = [e for e in events if e.get("event") == "dispatch_summary"]
    # Exactly one dispatch_summary event in the captured window.
    assert len(summary_events) == 1, (
        f"expected exactly one dispatch_summary event, got {len(summary_events)}; event names: {[e.get('event') for e in events]}"
    )


@pytest.mark.asyncio
async def test_sse_closes_on_complete(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status 'complete' -> SSE generator yields the complete event and returns."""
    _ac, _mock_router, redis_client = smoke
    monkeypatch.setattr("phaze.routers.execution.asyncio.sleep", AsyncMock(return_value=None))

    batch_id = uuid.uuid4()
    from datetime import UTC, datetime

    await redis_client.hset(
        f"exec:{batch_id}",
        mapping={
            "total": 5,
            "completed": 5,
            "failed": 0,
            "copied": 5,
            "verified": 5,
            "deleted": 5,
            "subjobs_completed": 1,
            "subjobs_expected": 1,
            "status": "complete",
            "started_at": datetime.now(UTC).isoformat(),
            "agent:agent-a:total": 5,
            "agent:agent-a:completed": 5,
            "agent:agent-a:failed": 0,
            "dispatch_summary": json.dumps([{"agent_id": "agent-a", "name": "Alpha", "total": 5, "chunks": 1}]),
        },
    )

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/execution/progress/{batch_id}",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": _build_app_stub(redis_client),
    }
    request = Request(scope=scope)  # type: ignore[arg-type]
    response = await execution.execution_progress(request, str(batch_id))
    events = await _consume_sse(response.body_iterator, max_events=20)
    event_names = [e.get("event") for e in events]
    assert "complete" in event_names
    # Generator MUST close after the terminal event (no infinite stream).
    # _consume_sse caps at max_events; assert we didn't hit the cap.
    assert len(events) < 20, "SSE generator did not close after terminal 'complete' status"


@pytest.mark.asyncio
async def test_sse_closes_on_complete_with_errors(
    smoke: tuple[AsyncClient, AsyncMock, redis_async.Redis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """28-V-20: Status 'complete_with_errors' -> SSE yields that event and returns."""
    _ac, _mock_router, redis_client = smoke
    monkeypatch.setattr("phaze.routers.execution.asyncio.sleep", AsyncMock(return_value=None))

    batch_id = uuid.uuid4()
    from datetime import UTC, datetime

    await redis_client.hset(
        f"exec:{batch_id}",
        mapping={
            "total": 5,
            "completed": 3,
            "failed": 2,
            "copied": 5,
            "verified": 5,
            "deleted": 3,
            "subjobs_completed": 1,
            "subjobs_expected": 1,
            "status": "complete_with_errors",
            "started_at": datetime.now(UTC).isoformat(),
            "agent:agent-a:total": 5,
            "agent:agent-a:completed": 3,
            "agent:agent-a:failed": 2,
            "dispatch_summary": json.dumps([{"agent_id": "agent-a", "name": "Alpha", "total": 5, "chunks": 1}]),
        },
    )

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/execution/progress/{batch_id}",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": _build_app_stub(redis_client),
    }
    request = Request(scope=scope)  # type: ignore[arg-type]
    response = await execution.execution_progress(request, str(batch_id))
    events = await _consume_sse(response.body_iterator, max_events=20)
    event_names = [e.get("event") for e in events]
    assert "complete_with_errors" in event_names
    assert len(events) < 20, "SSE generator did not close after terminal 'complete_with_errors' status"


# ---------------------------------------------------------------------------
# Internal stub helpers
# ---------------------------------------------------------------------------


def _build_app_stub(redis_client: redis_async.Redis) -> object:
    """Minimal Starlette ASGI app stub exposing ``state.redis`` for the SSE handler."""

    class _AppStub:
        class _State:
            redis = redis_client

        state = _State()

    return _AppStub()
