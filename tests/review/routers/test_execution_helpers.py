"""Unit tests for execution router helpers (Phase 28 coverage fill).

Covers the small pure helpers in ``phaze.routers.execution`` that integration
tests step over: ``_coerce_int`` edge cases, ``_render_partial`` memoryview
body branch, and the SSE generator's "waiting" + malformed-JSON fallbacks.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import FastAPI
from fastapi.responses import Response
from httpx import ASGITransport, AsyncClient
import pytest
from starlette.requests import Request

from phaze.routers import execution
from phaze.routers.execution import _agents_view_from_hash, _build_agents_view, _coerce_int, _render_partial
from phaze.schemas.agent_tasks import ExecuteBatchProposalItem


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ---------------------------------------------------------------------------
# _coerce_int — pure-function unit tests (lines 218-227)
# ---------------------------------------------------------------------------


def test_coerce_int_none_returns_default() -> None:
    """None -> default value (line 219)."""
    assert _coerce_int(None) == 0
    assert _coerce_int(None, default=42) == 42


def test_coerce_int_int_returns_value() -> None:
    """Pass-through for native int."""
    assert _coerce_int(7) == 7
    assert _coerce_int(0) == 0
    assert _coerce_int(-3) == -3


def test_coerce_int_numeric_string_parses() -> None:
    """Numeric strings parse to int."""
    assert _coerce_int("17") == 17
    assert _coerce_int("0") == 0


def test_coerce_int_invalid_string_returns_default() -> None:
    """Non-numeric strings fall back to default (lines 225-226)."""
    assert _coerce_int("abc") == 0
    assert _coerce_int("abc", default=99) == 99
    assert _coerce_int("") == 0


def test_coerce_int_other_types_return_default() -> None:
    """Non-int/non-str/non-None objects fall back to default (line 227)."""
    assert _coerce_int(3.7) == 0  # float not coerced
    assert _coerce_int([1, 2]) == 0
    assert _coerce_int({"x": 1}) == 0
    assert _coerce_int(object(), default=11) == 11


# ---------------------------------------------------------------------------
# _agents_view_from_hash — uses _coerce_int for every numeric field (sanity)
# ---------------------------------------------------------------------------


def test_agents_view_pulls_counts_from_hash() -> None:
    """End-to-end through _coerce_int for the SSE per-agent rollup."""
    data = {
        "agent:agent-a:completed": "5",
        "agent:agent-a:failed": "1",
        "agent:agent-a:total": "10",
    }
    summary = [{"agent_id": "agent-a", "name": "Agent A", "total": 10}]
    rows = _agents_view_from_hash(data, summary)

    assert rows == [
        {
            "agent_id": "agent-a",
            "name": "Agent A",
            "completed": 5,
            "failed": 1,
            "total": 10,
        }
    ]


def test_agents_view_falls_back_to_dispatch_summary_total() -> None:
    """When ``agent:<id>:total`` is missing on the hash, fall back to summary total."""
    data: dict[str, str] = {}
    summary = [{"agent_id": "agent-a", "name": "Agent A", "total": 7}]
    rows = _agents_view_from_hash(data, summary)
    assert rows[0]["total"] == 7


# ---------------------------------------------------------------------------
# _render_partial — memoryview body branch (lines 264-265)
# ---------------------------------------------------------------------------


def _fake_request() -> Request:
    """Minimal ASGI request stub for templates that reference ``request``."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": None,
    }
    return Request(scope=scope)  # type: ignore[arg-type]


def test_render_partial_handles_memoryview_body() -> None:
    """Some Starlette versions hand back a memoryview body; helper must coerce to bytes (line 265)."""
    response = MagicMock(spec=Response)
    response.body = memoryview(b"<div>hello</div>")
    with patch.object(execution.templates, "TemplateResponse", return_value=response):
        out = _render_partial(_fake_request(), "execution/partials/progress.html", {"x": 1})
    assert out == "<div>hello</div>"


def test_render_partial_handles_bytes_body() -> None:
    """Standard case: response.body is bytes (no memoryview coercion needed)."""
    response = MagicMock(spec=Response)
    response.body = b"<span>ok</span>"
    with patch.object(execution.templates, "TemplateResponse", return_value=response):
        out = _render_partial(_fake_request(), "execution/partials/progress.html", {})
    assert out == "<span>ok</span>"


# ---------------------------------------------------------------------------
# SSE generator: empty hash + malformed dispatch_summary JSON
# Hits lines 289-292 (waiting event) and 300-301 (JSONDecodeError fallback).
# ---------------------------------------------------------------------------


@pytest.fixture
def smoke_sse_app() -> AsyncGenerator[tuple[FastAPI, MagicMock]]:
    """Minimal FastAPI app exposing /execution/progress/{batch_id} with a fake Redis client."""
    app = FastAPI()
    app.include_router(execution.router)
    redis = MagicMock()
    redis.hgetall = AsyncMock(return_value={})
    app.state.redis = redis
    app.state.task_router = MagicMock()
    app.state.queue = MagicMock()
    yield app, redis


async def test_sse_emits_waiting_when_hash_absent(smoke_sse_app: tuple[FastAPI, MagicMock]) -> None:
    """Empty Redis hash -> SSE emits 'Waiting for execution to start...' event (lines 289-291).

    The 'waiting' branch loops forever (``continue``), so the test arranges for
    Redis to return ``{}`` on the first call (triggers the waiting path) and a
    terminal hash on the second call so the generator returns cleanly.
    """
    app, redis = smoke_sse_app

    calls = 0

    async def fake_hgetall(_: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {}  # forces the "Waiting for execution..." continue branch
        return {
            "total": "0",
            "completed": "0",
            "failed": "0",
            "status": "complete",  # terminal -> generator returns after this tick
            "dispatch_summary": "[]",
        }

    redis.hgetall = fake_hgetall  # AsyncMock-compatible: hgetall is an async def
    with patch("phaze.routers.execution.asyncio.sleep", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/execution/progress/batch-xyz") as resp:
                assert resp.status_code == 200
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    assert b"Waiting for execution to start" in body
    assert b"event: progress" in body
    assert calls >= 2  # waiting branch fired at least once, then terminal hash closed the stream


async def test_sse_empty_hash_terminates_after_cap(smoke_sse_app: tuple[FastAPI, MagicMock]) -> None:
    """phaze-5zyv: a hash that never appears -> stream emits a terminal 'complete' and returns, not an infinite wait."""
    app, redis = smoke_sse_app
    # hgetall always returns {} -- an empty dispatch, reaped batch, or unknown batch_id.
    redis.hgetall = AsyncMock(return_value={})
    with patch("phaze.routers.execution.asyncio.sleep", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/execution/progress/never-seeded") as resp:
                assert resp.status_code == 200
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    # The generator closed on its own: a terminal 'complete' event was emitted and the stream ended.
    assert b"event: complete" in body
    assert b"no longer available" in body
    # It did not poll forever: the empty-hash cap is small, so hgetall was called a bounded number
    # of times (the cap), not unboundedly.
    from phaze.routers.execution import _MAX_EMPTY_POLLS

    assert redis.hgetall.await_count == _MAX_EMPTY_POLLS


async def test_sse_falls_back_when_dispatch_summary_is_malformed_json(
    smoke_sse_app: tuple[FastAPI, MagicMock],
) -> None:
    """Malformed dispatch_summary JSON falls back to [] without raising (lines 300-301)."""
    app, redis = smoke_sse_app
    redis.hgetall = AsyncMock(
        return_value={
            "total": "3",
            "completed": "3",
            "failed": "0",
            "status": "complete",  # terminal -> generator closes after one tick
            "dispatch_summary": "{not-valid-json",  # malformed
        }
    )
    # Render through real Jinja but skip the sleep.
    with patch("phaze.routers.execution.asyncio.sleep", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/execution/progress/batch-xyz") as resp:
                assert resp.status_code == 200
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    # Generator did NOT raise (would have returned a 500 or closed without events).
    # We expect a normal SSE stream that includes the agents_table event (rendered with
    # an empty agents list because dispatch_summary fell back to []).
    assert b"event: agents_table" in body
    # And it should have closed normally with a complete event.
    assert b"event: complete" in body
    # Sanity: no JSONDecodeError leaked into the stream.
    assert b"JSONDecodeError" not in body


# Quick sanity check: malformed JSON path was triggered (verify by passing valid JSON for contrast).
async def test_sse_with_valid_dispatch_summary_succeeds(
    smoke_sse_app: tuple[FastAPI, MagicMock],
) -> None:
    """Control: valid JSON dispatch_summary renders without falling through to the except branch."""
    app, redis = smoke_sse_app
    redis.hgetall = AsyncMock(
        return_value={
            "total": "1",
            "completed": "1",
            "failed": "0",
            "status": "complete",
            "dispatch_summary": json.dumps([{"agent_id": "a", "name": "A", "total": 1}]),
        }
    )
    with patch("phaze.routers.execution.asyncio.sleep", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/execution/progress/batch-xyz") as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    assert b"event: agents_table" in body
    assert b"event: complete" in body


# ---------------------------------------------------------------------------
# _build_agents_view — direct unit test (lines 70-80)
#
# Integration tests reach this through ``start_execution``, but those require
# real Postgres + Redis. Direct unit test ensures coverage when the smoke
# suite cannot run.
# ---------------------------------------------------------------------------


def _proposal(agent_id: str = "agent-a") -> ExecuteBatchProposalItem:
    """Helper: tiny ExecuteBatchProposalItem with all required fields."""
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        original_path=f"/in/{agent_id}.mp3",
        proposed_path="out",  # RELATIVE destination directory
        proposed_filename=f"{agent_id}.mp3",
    )


def test_build_agents_view_default_names_falls_back_to_agent_id() -> None:
    """No ``agent_names`` provided -> rows show agent_id in the ``name`` slot (line 70 fallback)."""
    groups = {"agent-a": [_proposal("agent-a"), _proposal("agent-a")], "agent-b": [_proposal("agent-b")]}
    rows = _build_agents_view(groups)
    assert rows == [
        {"agent_id": "agent-a", "name": "agent-a", "completed": 0, "failed": 0, "total": 2},
        {"agent_id": "agent-b", "name": "agent-b", "completed": 0, "failed": 0, "total": 1},
    ]


def test_build_agents_view_uses_provided_names() -> None:
    """``agent_names`` dict supplies display labels; missing entries fall back to agent_id."""
    groups = {"agent-a": [_proposal("agent-a")], "agent-b": [_proposal("agent-b")]}
    rows = _build_agents_view(groups, agent_names={"agent-a": "Agent Alpha"})
    assert rows[0]["name"] == "Agent Alpha"
    assert rows[1]["name"] == "agent-b"  # missing -> falls back


def test_build_agents_view_empty_groups_returns_empty_list() -> None:
    """No agent groups -> no rows. Avoids div-by-zero in downstream renderers."""
    assert _build_agents_view({}) == []
    assert _build_agents_view({}, agent_names={"agent-a": "x"}) == []


# ---------------------------------------------------------------------------
# start_execution: enqueue-failure best-effort log-and-continue (lines 181-187)
#
# Integration tests in test_execution_dispatch.py only exercise the happy
# path. This unit test patches the dispatch-service helpers + redis + task
# router so a SAQ enqueue failure is forced, asserting the dispatch does NOT
# abort and that the failure is logged via ``logger.exception``.
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_app() -> tuple[FastAPI, AsyncMock, MagicMock]:
    """Smoke FastAPI app with a mock task_router + redis pipeline for /execution/start."""
    from phaze.database import get_session

    app = FastAPI()
    app.include_router(execution.router)

    # Mock DB session: detect_collisions + the Agent display-name query both
    # call session.execute. The values are irrelevant for the enqueue-failure
    # test (collisions=[] is enforced by patching detect_collisions below).
    session = AsyncMock()
    # session.execute(...) returns a Result-like object; for the agent-name
    # query the code calls ``.all()`` on it; for the rest it does not matter.
    name_result = MagicMock()
    name_result.all.return_value = []
    session.execute.return_value = name_result
    app.dependency_overrides[get_session] = lambda: session

    mock_router = AsyncMock()
    app.state.task_router = mock_router

    redis_client = MagicMock()
    pipe = AsyncMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=None)
    pipe.hset = MagicMock()
    pipe.hincrby = MagicMock()
    pipe.expire = MagicMock()
    pipe.delete = MagicMock()
    pipe.execute = AsyncMock(return_value=None)
    redis_client.pipeline = MagicMock(return_value=pipe)
    # phaze-fa2p: the single-dispatch guard claims ``exec:active`` via SET NX before seeding. Default
    # to "claim won" so the enqueue-failure/reconcile tests exercise a live dispatch; the double-
    # dispatch rejection is covered by its own test that overrides ``.set`` to return None.
    redis_client.set = AsyncMock(return_value=True)
    app.state.redis = redis_client
    app.state.queue = MagicMock()

    return app, mock_router, redis_client


async def test_start_execution_logs_and_continues_on_enqueue_failure(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """task_router.enqueue_for_agent raising -> ``logger.exception`` fires, dispatch continues (lines 181-187)."""
    app, mock_router, _redis = dispatch_app
    # One agent, two proposals -> single chunk -> single enqueue attempt that raises.
    groups = {"agent-a": [_proposal("agent-a"), _proposal("agent-a")]}

    mock_router.enqueue_for_agent = AsyncMock(side_effect=RuntimeError("redis broke mid-enqueue"))

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch(
            "phaze.routers.execution.get_approved_proposals_grouped_by_agent",
            AsyncMock(return_value=groups),
        ),
        patch(
            "phaze.routers.execution.count_revoked_skipped_proposals",
            AsyncMock(return_value=0),
        ),
        caplog.at_level("ERROR", logger="phaze.routers.execution"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    # Dispatch did NOT raise; route returned the progress card HTML.
    assert resp.status_code == 200
    assert 'id="execution-progress"' in resp.text or "Execution progress" in resp.text or resp.text.strip()
    # Enqueue was attempted and the exception path was taken.
    assert mock_router.enqueue_for_agent.await_count == 1
    assert any("dispatch: enqueue failed" in r.message for r in caplog.records)


def _hset_calls(pipe: AsyncMock) -> list[tuple]:
    """Positional args of every pipe.hset(...) call (batch seed + correction)."""
    return [c.args for c in pipe.hset.call_args_list]


async def test_start_execution_zero_enqueues_reaches_terminal_status(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """Every chunk failing to enqueue -> batch is promoted to a terminal status, not stuck 'running' (phaze-kxsb)."""
    app, mock_router, redis_client = dispatch_app
    pipe = redis_client.pipeline.return_value
    groups = {"agent-a": [_proposal("agent-a"), _proposal("agent-a")]}

    mock_router.enqueue_for_agent = AsyncMock(side_effect=RuntimeError("broker down"))

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    assert resp.status_code == 200
    # subjobs_expected corrected to 0 and status HSET to a terminal value in the correction pipeline.
    hsets = _hset_calls(pipe)
    assert any(len(a) >= 3 and a[1] == "subjobs_expected" and a[2] == "0" for a in hsets)
    assert any(len(a) >= 3 and a[1] == "status" and a[2] == "complete_with_errors" for a in hsets)
    # Undispatched proposals surfaced as failed, and the terminal status closes the card.
    assert pipe.hincrby.call_args.args[1] == "failed"
    assert pipe.hincrby.call_args.args[2] == 2


async def test_start_execution_partial_enqueue_failure_corrects_expected(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """One chunk lands, one fails -> subjobs_expected corrected to 1 and the promote check re-runs (phaze-kxsb)."""
    app, mock_router, redis_client = dispatch_app
    pipe = redis_client.pipeline.return_value
    groups = {"agent-a": [_proposal("agent-a")], "agent-b": [_proposal("agent-b")]}

    async def _enqueue(*, agent_id: str, **_kw: Any) -> None:
        if agent_id == "agent-b":
            raise RuntimeError("agent-b broker down")

    mock_router.enqueue_for_agent = AsyncMock(side_effect=_enqueue)
    promote = AsyncMock()

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
        patch("phaze.routers.execution._get_promote_status_script", MagicMock(return_value=promote)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    assert resp.status_code == 200
    # subjobs_expected corrected to the 1 that landed; status left 'running' (a landed sub-job will POST).
    hsets = _hset_calls(pipe)
    assert any(len(a) >= 3 and a[1] == "subjobs_expected" and a[2] == "1" for a in hsets)
    assert not any(len(a) >= 3 and a[1] == "status" and a[2] == "complete_with_errors" for a in hsets)
    # The promote check re-ran to close the race where the landed sub-job already reported terminal.
    promote.assert_awaited_once()
    assert pipe.hincrby.call_args.args[2] == 1  # one undispatched proposal counted as failed
    # phaze-1h6j: the failing agent's PER-AGENT failed counter is also incremented so its row can
    # reach a terminal pill (batch-level failed alone left it stuck RUNNING). agent-b lost its chunk.
    hincrbys = [c.args for c in pipe.hincrby.call_args_list]
    assert any(len(a) >= 3 and a[1] == "agent:agent-b:failed" and a[2] == 1 for a in hincrbys)
    # agent-a landed, so it must NOT get a spurious per-agent failed increment.
    assert not any(len(a) >= 3 and a[1] == "agent:agent-a:failed" for a in hincrbys)


async def test_start_execution_skips_redis_seed_when_no_groups(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """No approved proposals -> redis pipeline is NOT entered (line 157 ``if groups:`` False branch)."""
    app, mock_router, redis_client = dispatch_app

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch(
            "phaze.routers.execution.get_approved_proposals_grouped_by_agent",
            AsyncMock(return_value={}),
        ),
        patch(
            "phaze.routers.execution.count_revoked_skipped_proposals",
            AsyncMock(return_value=0),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    assert resp.status_code == 200
    mock_router.enqueue_for_agent.assert_not_awaited()
    redis_client.pipeline.assert_not_called()
    # phaze-fa2p: an empty dispatch enqueues nothing, so it never claims the single-dispatch sentinel.
    redis_client.set.assert_not_awaited()


async def test_start_execution_rejected_when_dispatch_already_active(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """A losing SET NX on ``exec:active`` -> the dispatch is refused, nothing is seeded or enqueued (phaze-fa2p)."""
    app, mock_router, redis_client = dispatch_app
    # Simulate a concurrent/active dispatch already holding the sentinel: SET NX returns None.
    redis_client.set = AsyncMock(return_value=None)
    groups = {"agent-a": [_proposal("agent-a"), _proposal("agent-a")]}

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    assert resp.status_code == 200
    assert "Execution already in progress" in resp.text
    # The guard fires BEFORE any seed or enqueue -- no double-dispatch.
    redis_client.set.assert_awaited_once()
    redis_client.pipeline.assert_not_called()
    mock_router.enqueue_for_agent.assert_not_awaited()


async def test_start_execution_returns_collision_block_when_destinations_collide(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """Collisions present -> collision_block.html short-circuits dispatch (no enqueue, no redis seed)."""
    app, mock_router, redis_client = dispatch_app

    with patch(
        "phaze.routers.execution.detect_collisions",
        AsyncMock(return_value=[{"destination_path": "/x.mp3", "proposals": []}]),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    assert resp.status_code == 200
    # Collision short-circuit means dispatch helpers should NEVER be touched.
    mock_router.enqueue_for_agent.assert_not_awaited()
    redis_client.pipeline.assert_not_called()
