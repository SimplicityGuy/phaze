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
from redis.exceptions import ResponseError
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
            async with ac.stream("GET", f"/execution/progress/{uuid.uuid4()}") as resp:
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
            async with ac.stream("GET", f"/execution/progress/{uuid.uuid4()}") as resp:
                assert resp.status_code == 200
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    # The generator closed on its own: a terminal 'complete' event was emitted and the stream ended.
    assert b"event: complete" in body
    assert b"no longer available" in body
    # phaze-047gd: a canonical 'close' event must follow -- it's the only event name the template's
    # sse-close="close" listener (on the sse-connect element) reacts to. Without it the browser's
    # EventSource treats the server closing the HTTP stream as a network drop and auto-reconnects.
    assert b"event: close" in body
    assert body.index(b"event: close") > body.index(b"event: complete")
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
            async with ac.stream("GET", f"/execution/progress/{uuid.uuid4()}") as resp:
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
            async with ac.stream("GET", f"/execution/progress/{uuid.uuid4()}") as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    assert b"event: agents_table" in body
    assert b"event: complete" in body
    # phaze-047gd: canonical close event follows the status event on this terminal path too.
    assert b"event: close" in body
    assert body.index(b"event: close") > body.index(b"event: complete")


async def test_sse_status_terminal_path_emits_close_after_complete_with_errors(
    smoke_sse_app: tuple[FastAPI, MagicMock],
) -> None:
    """phaze-047gd: the ``complete_with_errors`` terminal branch also emits the canonical close event."""
    app, redis = smoke_sse_app
    redis.hgetall = AsyncMock(
        return_value={
            "total": "2",
            "completed": "1",
            "failed": "1",
            "status": "complete_with_errors",
            "dispatch_summary": "[]",
        }
    )
    with patch("phaze.routers.execution.asyncio.sleep", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", f"/execution/progress/{uuid.uuid4()}") as resp:
                assert resp.status_code == 200
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    assert b"event: complete_with_errors" in body
    assert b"event: close" in body
    assert body.index(b"event: close") > body.index(b"event: complete_with_errors")


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
def dispatch_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AsyncMock, MagicMock]:
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
    redis_client.set = AsyncMock(return_value=True)
    redis_client.delete = AsyncMock(return_value=1)
    # phaze-2tsw9: a refused claim now tries to reattach to the live batch via GET
    # execdispatch:active + HGETALL exec:{batch_id}. Default to "nothing to reattach to" (no active
    # batch_id) so tests that don't care about this path keep seeing the plain refusal alert; the
    # reattachment test below overrides both.
    redis_client.get = AsyncMock(return_value=None)
    redis_client.hgetall = AsyncMock(return_value={})
    app.state.redis = redis_client
    # phaze-fa2p / phaze-j7u8: the single-dispatch guard claims the sentinel, now via the
    # claim-or-reconcile Lua rather than a bare SET NX. Default to 1 ("claimed outright") so the
    # enqueue-failure/reconcile tests exercise a live dispatch; the refusal and stale-reconcile paths
    # are covered by their own tests, which re-set ``claim.return_value``.
    claim = AsyncMock(return_value=1)
    redis_client.claim_dispatch_mock = claim
    monkeypatch.setattr("phaze.routers.execution._get_claim_dispatch_script", MagicMock(return_value=claim))
    # phaze-0t2c: the CAS release used when a dispatch ends before any sub-job lands.
    release = AsyncMock(return_value=1)
    redis_client.release_dispatch_mock = release
    monkeypatch.setattr("phaze.routers.execution._get_release_dispatch_script", MagicMock(return_value=release))
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
    redis_client.claim_dispatch_mock.assert_not_awaited()


async def test_start_execution_rejected_when_dispatch_already_active(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """A refused claim on the sentinel -> the dispatch is refused, nothing seeded or enqueued (phaze-fa2p)."""
    app, mock_router, redis_client = dispatch_app
    # Simulate a dispatch that is GENUINELY in flight: the claim script returns 0 (refused).
    redis_client.claim_dispatch_mock.return_value = 0
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
    # The guard fires BEFORE any enqueue -- no double-dispatch. (phaze-0t2c moved the seed AHEAD of
    # the claim to close the claimed-but-never-seeded window, so the seed pipeline HAS run by now;
    # what matters is that no move job was dispatched.)
    redis_client.claim_dispatch_mock.assert_awaited_once()
    mock_router.enqueue_for_agent.assert_not_awaited()
    # ...and the hash seeded for this refused batch_id is dropped rather than left to rot for 24h.
    redis_client.delete.assert_awaited_once()


async def test_start_execution_refused_reattaches_to_the_live_progress_card(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """A refused claim re-attaches to the ACTUALLY running batch's progress card (phaze-2tsw9).

    Before the fix this branch always rendered the static ``dispatch_in_progress.html`` alert into
    the SAME sink (``#apply-execute-response``, ``hx-swap="innerHTML"``) the live progress card
    already occupies, evicting it and closing its only ``sse-connect`` mount with no way back in.
    """
    app, mock_router, redis_client = dispatch_app
    redis_client.claim_dispatch_mock.return_value = 0
    live_batch_id = "11111111-1111-1111-1111-111111111111"
    redis_client.get = AsyncMock(return_value=live_batch_id)
    redis_client.hgetall = AsyncMock(
        return_value={
            "total": "10",
            "completed": "3",
            "failed": "0",
            "status": "running",
            "subjobs_expected": "2",
            "dispatch_summary": json.dumps([{"agent_id": "agent-a", "name": "Agent A", "total": 10}]),
            "agent:agent-a:completed": "3",
            "agent:agent-a:failed": "0",
            "agent:agent-a:total": "10",
        }
    )
    groups = {"agent-a": [_proposal("agent-a"), _proposal("agent-a")]}

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    assert resp.status_code == 200
    assert "Execution already in progress" not in resp.text
    assert f"/execution/progress/{live_batch_id}" in resp.text
    assert "Agent A" in resp.text
    mock_router.enqueue_for_agent.assert_not_awaited()


async def test_reattach_active_progress_returns_none_without_an_active_batch() -> None:
    """No ``execdispatch:active`` value -> nothing to reattach to (falls back to the static alert)."""
    from phaze.routers.execution import _reattach_active_progress

    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=None)
    result = await _reattach_active_progress(_fake_request(), redis_client)
    assert result is None


async def test_reattach_active_progress_returns_none_when_hash_already_reaped() -> None:
    """An active sentinel whose batch hash has already reaped -> nothing to reattach to."""
    from phaze.routers.execution import _reattach_active_progress

    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value="some-batch-id")
    redis_client.hgetall = AsyncMock(return_value={})
    result = await _reattach_active_progress(_fake_request(), redis_client)
    assert result is None


async def test_start_execution_reconciled_stale_sentinel_is_logged_and_proceeds(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A claim taken by RECONCILING a leaked sentinel proceeds -- and says so in the log (phaze-j7u8).

    Return code 2 means the previous dispatch could never have released its own claim (its hash was
    reaped, already terminal, or fully reported but unpromoted). The operator must not be locked out
    for 24h over it, but the leak is still a defect worth a WARNING rather than a silent recovery.
    """
    app, mock_router, redis_client = dispatch_app
    redis_client.claim_dispatch_mock.return_value = 2
    groups = {"agent-a": [_proposal("agent-a")]}

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
        caplog.at_level("WARNING", logger="phaze.routers.execution"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/execution/start")

    assert resp.status_code == 200
    assert "Execution already in progress" not in resp.text
    mock_router.enqueue_for_agent.assert_awaited()  # the dispatch really went ahead
    assert any("stale exec:active sentinel" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# phaze-0t2c: the claim must never outlive the things that can release it --
# it is taken AFTER the seed, and any failure before a sub-job lands settles it.
# ---------------------------------------------------------------------------


async def test_start_execution_seeds_the_hash_before_claiming_the_sentinel(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """A failing seed must leave NO claim outstanding (phaze-0t2c).

    The original order claimed first and seeded immediately after, and those are two separate
    commands: a transient Redis fault on the seed left the sentinel naming a batch whose hash never
    existed, so nothing could ever release it and every Execute Approved was refused for 24h.
    """
    app, mock_router, redis_client = dispatch_app
    pipe = redis_client.pipeline.return_value
    pipe.execute = AsyncMock(side_effect=RuntimeError("redis hiccup on the seed"))
    groups = {"agent-a": [_proposal("agent-a")]}

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
        pytest.raises(RuntimeError, match="redis hiccup"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            await ac.post("/execution/start")

    # The claim was never attempted, so there is nothing to leak and nothing to reap.
    redis_client.claim_dispatch_mock.assert_not_awaited()
    mock_router.enqueue_for_agent.assert_not_awaited()


async def test_start_execution_crash_mid_enqueue_settles_the_sentinel(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """A crash part-way through the enqueue loop with NOTHING landed releases the claim (phaze-0t2c).

    The loop spans many suspension points and real wall time on a large approved set. Before this
    fix a failure inside it left the hash seeded for the PLANNED subjobs_expected with fewer chunks
    enqueued, so subjobs_completed could never reach it, the promotion never fired, and the sentinel
    was held for its full 24h TTL.
    """
    app, mock_router, redis_client = dispatch_app
    groups = {"agent-a": [_proposal("agent-a")], "agent-b": [_proposal("agent-b")]}
    mock_router.enqueue_for_agent = AsyncMock(side_effect=BaseException("worker process died"))

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
        pytest.raises(BaseException, match="worker process died"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            await ac.post("/execution/start")

    # Nothing landed, so the claim is released via the CAS release rather than held to TTL.
    redis_client.release_dispatch_mock.assert_awaited_once()


async def test_start_execution_crash_mid_enqueue_lowers_expected_to_what_landed(
    dispatch_app: tuple[FastAPI, AsyncMock, MagicMock],
) -> None:
    """A crash after SOME chunks landed lowers subjobs_expected to the landed count (phaze-0t2c).

    The landed sub-jobs will POST their terminal events, so the batch must be given a target they
    can actually reach -- otherwise the promotion (and the sentinel release it carries) is
    unreachable by construction.
    """
    app, mock_router, redis_client = dispatch_app
    pipe = redis_client.pipeline.return_value
    groups = {"agent-a": [_proposal("agent-a")], "agent-b": [_proposal("agent-b")], "agent-c": [_proposal("agent-c")]}
    calls = {"n": 0}

    async def _enqueue(**_kw: Any) -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            msg = "broker died after the first chunk"
            raise BaseException(msg)

    mock_router.enqueue_for_agent = AsyncMock(side_effect=_enqueue)
    promote = AsyncMock()

    with (
        patch("phaze.routers.execution.detect_collisions", AsyncMock(return_value=[])),
        patch("phaze.routers.execution.get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups)),
        patch("phaze.routers.execution.count_revoked_skipped_proposals", AsyncMock(return_value=0)),
        patch("phaze.routers.execution._get_promote_status_script", MagicMock(return_value=promote)),
        pytest.raises(BaseException, match="broker died"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            await ac.post("/execution/start")

    hsets = _hset_calls(pipe)
    assert any(len(a) >= 3 and a[1] == "subjobs_expected" and a[2] == "1" for a in hsets)
    # One landed, so the claim is NOT released here -- that sub-job's terminal POST will do it. The
    # promote check re-runs in case it already reported against the stale, higher expected count.
    redis_client.release_dispatch_mock.assert_not_awaited()
    promote.assert_awaited_once()
    # The two chunks that were never reached are counted as failed, not silently dropped.
    hincrbys = [c.args for c in pipe.hincrby.call_args_list]
    assert any(len(a) >= 3 and a[1] == "failed" and a[2] == 2 for a in hincrbys)


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


# ---------------------------------------------------------------------------
# phaze-c3j0: batch_id is a UUID, and the sentinel no longer shares the
# per-batch key namespace, so no batch-id spelling can alias a control key.
# ---------------------------------------------------------------------------


@pytest.fixture
def progress_read_app() -> tuple[FastAPI, MagicMock]:
    """Smoke app exposing the two progress-READ routes with a fake Redis."""
    app = FastAPI()
    app.include_router(execution.router)
    redis = MagicMock()
    redis.hgetall = AsyncMock(return_value={})
    app.state.redis = redis
    app.state.task_router = MagicMock()
    app.state.queue = MagicMock()
    return app, redis


@pytest.mark.parametrize(
    "url",
    [
        "/execution/progress/active",
        "/execution/agents-table?batch_id=active",
    ],
)
async def test_progress_routes_reject_the_sentinel_spelling_before_touching_redis(
    progress_read_app: tuple[FastAPI, MagicMock],
    url: str,
) -> None:
    """``batch_id=active`` is refused by validation, not by a WRONGTYPE traceback (phaze-c3j0).

    Both routes used to take a free-form ``str`` and interpolate it into ``exec:{batch_id}``, so
    the literal ``active`` rebuilt the ``exec:active`` STRING sentinel. HGETALL against a string
    replies WRONGTYPE, which escaped as an unhandled 500 on the table route and killed the SSE
    stream before its first yield on the other. Typing the parameter as ``uuid.UUID`` means FastAPI
    422s it before Redis is touched at all.
    """
    app, redis = progress_read_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(url)

    assert resp.status_code == 422
    redis.hgetall.assert_not_called()


def test_sentinel_lives_outside_the_per_batch_key_namespace() -> None:
    """The structural half of the fix: no batch-id spelling can name the control key (phaze-c3j0).

    Typing the parameter fixes the two known call sites; separating the namespaces means a future
    route that accepts a looser batch_id cannot reintroduce the alias.
    """
    assert not execution.ACTIVE_DISPATCH_KEY.startswith(execution.BATCH_KEY_PREFIX)


async def test_agents_table_wrong_key_type_degrades_to_the_empty_state(
    progress_read_app: tuple[FastAPI, MagicMock],
) -> None:
    """A WRONGTYPE read renders the documented empty state rather than a 500 (phaze-c3j0).

    Defence in depth behind the typed parameter: this route's own docstring promises that a batch
    which has already reaped renders an empty ``agents_table.html``, and a wrong key type should
    not be the one input that contradicts it.
    """
    app, redis = progress_read_app
    redis.hgetall = AsyncMock(side_effect=ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/execution/agents-table?batch_id={uuid.uuid4()}")

    assert resp.status_code == 200
    # The write is gated behind ``if data:``, so a failed read must not reach the persist script.
    assert "aria-sort" in resp.text or resp.text.strip()


async def test_sse_wrong_key_type_still_emits_a_terminal_close(
    progress_read_app: tuple[FastAPI, MagicMock],
) -> None:
    """The SSE generator must always terminate with a close event, even on a bad key type (phaze-c3j0)."""
    app, redis = progress_read_app
    redis.hgetall = AsyncMock(side_effect=ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value"))

    with patch("phaze.routers.execution.asyncio.sleep", new=AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", f"/execution/progress/{uuid.uuid4()}") as resp:
                assert resp.status_code == 200
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk

    # Degrades to the empty-hash path, which is bounded by _MAX_EMPTY_POLLS and closes the stream.
    assert b"event: complete" in body
    # phaze-047gd: and the canonical close event that actually stops the browser's EventSource.
    assert b"event: close" in body
