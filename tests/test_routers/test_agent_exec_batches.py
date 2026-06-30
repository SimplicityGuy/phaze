"""Contract tests for POST /api/internal/agent/exec-batches/{batch_id}/progress (Phase 28 D-05, D-17).

Targets 28-V-10 .. 28-V-16. Mirrors:
- tests/test_routers/test_agent_scan_batches.py (smoke-app fixture; cross-tenant 403 + 404 ordering).
- tests/test_routers/test_agent_tracklists.py (Redis-backed idempotency dup-call test).

The endpoint contract (handler ordering is part of the spec):
  1. 401 if no bearer token.
  2. 403 if `body.agent_id != agent.id` (cross-tenant guard, fires BEFORE any Redis read).
  3. 404 if `exec:{batch_id}` hash absent (HEXISTS total).
  4. 403 if `agent:<body.agent_id>:total` rollup field absent (caller not in dispatch).
  5. SET NX EX `exec_progress_req:{request_id}` dedup -- duplicate returns 200 with no HINCRBY.
  6. HINCRBY counters per D-07 rules; sub_batch_terminal promotes status when subjobs_completed == subjobs_expected.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers import agent_exec_batches


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")

# Worktree-isolation note: Plan 28-02 runs in parallel with Plan 28-03. The two
# pytest processes share the default `phaze_test` Postgres database, and the
# project's `tests/conftest.py:async_engine` fixture races on inserting the
# `legacy-application-server` Agent row at fixture setup. To prevent the
# collision without modifying the shared conftest, we honour
# `PHAZE_TEST_DATABASE_URL_28_02` if set (the orchestrator/operator points
# this at a worktree-dedicated database) by monkeypatching the conftest
# module attribute BEFORE the `async_engine` fixture reads it.
_OVERRIDE_DB_URL = os.environ.get("PHAZE_TEST_DATABASE_URL_28_02")


@pytest.fixture(autouse=True)
def _override_test_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `tests.conftest.TEST_DATABASE_URL` at a worktree-dedicated DB if set."""
    if _OVERRIDE_DB_URL:
        import tests.conftest as _conftest

        monkeypatch.setattr(_conftest, "TEST_DATABASE_URL", _OVERRIDE_DB_URL)


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[redis_async.Redis]:
    """Real Redis client with decode_responses=True (matches the production wiring).

    Cleans up `exec:*` and `exec_progress_req:*` keys around each test so reruns
    do not collide. Uses scan_iter rather than KEYS for memory safety.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL, decode_responses=True)
    # Pre-clean (defensive in case prior runs leaked keys).
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


def _make_smoke_app(session: AsyncSession, redis_client: redis_async.Redis) -> FastAPI:
    """Smoke FastAPI app with the agent_exec_batches router + session override + redis on app.state."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_exec_batches.router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.redis = redis_client
    return app


def _make_client(
    session: AsyncSession,
    redis_client: redis_async.Redis,
    token: str | None = None,
) -> AsyncClient:
    app = _make_smoke_app(session, redis_client)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


async def _seed_exec_hash(
    redis_client: redis_async.Redis,
    batch_id: uuid.UUID,
    agent_id: str,
    *,
    total: int = 10,
    subjobs_expected: int = 1,
    subjobs_completed: int = 0,
    completed: int = 0,
    failed: int = 0,
    copied: int = 0,
    verified: int = 0,
    deleted: int = 0,
    status: str = "running",
    agent_total: int | None = None,
    extra_fields: dict[str, str | int] | None = None,
) -> None:
    """Seed an `exec:{batch_id}` hash matching the D-09 step 5 dispatch shape."""
    if agent_total is None:
        agent_total = total
    fields: dict[str, str | int] = {
        "total": total,
        "subjobs_expected": subjobs_expected,
        "subjobs_completed": subjobs_completed,
        "completed": completed,
        "failed": failed,
        "copied": copied,
        "verified": verified,
        "deleted": deleted,
        "status": status,
        f"agent:{agent_id}:total": agent_total,
        f"agent:{agent_id}:completed": 0,
        f"agent:{agent_id}:failed": 0,
    }
    if extra_fields:
        fields.update(extra_fields)
    await redis_client.hset(f"exec:{batch_id}", mapping=fields)  # type: ignore[arg-type]


def _make_progress_body(
    *,
    batch_id: uuid.UUID,
    agent_id: str,
    terminal_step: str = "deleted",
    failed_at_step: str | None = None,
    sub_batch_terminal: bool = False,
    request_id: uuid.UUID | None = None,
    sub_batch_index: int = 0,
) -> dict[str, object]:
    body: dict[str, object] = {
        "request_id": str(request_id or uuid.uuid4()),
        "batch_id": str(batch_id),
        "agent_id": agent_id,
        "sub_batch_index": sub_batch_index,
        "proposal_id": str(uuid.uuid4()),
        "terminal_step": terminal_step,
        "sub_batch_terminal": sub_batch_terminal,
    }
    if failed_at_step is not None:
        body["failed_at_step"] = failed_at_step
    return body


# ---------------------------------------------------------------------------
# 28-V-10: Unauthenticated -> 401
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unauthenticated_401(session: AsyncSession, redis_client: redis_async.Redis) -> None:
    """POST without Authorization header -> 401."""
    batch_id = uuid.uuid4()
    async with _make_client(session, redis_client, token=None) as ac:
        r = await ac.post(
            f"/api/internal/agent/exec-batches/{batch_id}/progress",
            json=_make_progress_body(batch_id=batch_id, agent_id="test-agent-01"),
        )
    assert r.status_code == 401


@pytest.mark.integration
async def test_unknown_token_403(session: AsyncSession, redis_client: redis_async.Redis) -> None:
    """Well-formed bearer token with unknown hash -> 403."""
    batch_id = uuid.uuid4()
    async with _make_client(session, redis_client, token="phaze_agent_unknown-token-1234") as ac:  # noqa: S106
        r = await ac.post(
            f"/api/internal/agent/exec-batches/{batch_id}/progress",
            json=_make_progress_body(batch_id=batch_id, agent_id="test-agent-01"),
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 28-V-11: Cross-tenant guard (body.agent_id != auth agent.id) -> 403 BEFORE Redis read
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cross_tenant_agent_id_mismatch_403_before_state_read(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """T-28-02-S1: body.agent_id != agent.id -> 403, even when the Redis hash DOES NOT EXIST.

    Proof of ordering: if the cross-tenant guard ran AFTER the 404 hash-exists
    check, this test would return 404 (no `exec:{batch_id}` hash seeded).
    The fact that it returns 403 proves the guard runs FIRST (D-17 step 2).
    """
    _agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    # Deliberately DO NOT seed the hash. If the guard runs AFTER 404, this is 404.
    body = _make_progress_body(batch_id=batch_id, agent_id="other-agent")

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 403, f"Expected 403 (cross-tenant guard FIRST), got {r.status_code}: {r.text}"
    assert "agent_id" in r.text.lower()
    assert "does not match" in r.text.lower() or "match" in r.text.lower()


# ---------------------------------------------------------------------------
# 28-V-12: Unknown batch_id -> 404
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_unknown_batch_404(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """exec:{batch_id} hash absent -> 404 'batch not found'."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()  # never seeded
    body = _make_progress_body(batch_id=batch_id, agent_id=agent.id)

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 404
    assert "not found" in r.text.lower()


# ---------------------------------------------------------------------------
# 28-V-13: Non-participating agent (per-agent rollup absent) -> 403
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_non_participating_agent_403(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Hash exists with `total` but no `agent:<my-id>:total` rollup -> 403 (D-17 step 4)."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    # Seed the hash for a DIFFERENT agent so the rollup field for `agent.id` is absent.
    await _seed_exec_hash(redis_client, batch_id, agent_id="some-other-fileserver")

    body = _make_progress_body(batch_id=batch_id, agent_id=agent.id)
    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 403
    assert "dispatch" in r.text.lower() or "not part" in r.text.lower()


# ---------------------------------------------------------------------------
# 28-V-14: Idempotent dup request_id -> 200, no double HINCRBY
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_duplicate_request_id_does_not_re_increment(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Same request_id -> 200, completed counter incremented only once."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent.id)
    request_id = uuid.uuid4()
    body = _make_progress_body(
        batch_id=batch_id,
        agent_id=agent.id,
        terminal_step="deleted",
        request_id=request_id,
    )

    async with _make_client(session, redis_client, raw_token) as ac:
        r1 = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)
        r2 = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # Counter incremented exactly once even though two requests landed.
    completed = await redis_client.hget(f"exec:{batch_id}", "completed")
    assert completed == "1", f"completed counter should be 1 after dedup, got {completed!r}"


# ---------------------------------------------------------------------------
# 28-V-15: Counter math (D-07 rules) — all four terminal_step branches + 3 failed_at_step paths
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_counter_math_terminal_step_deleted(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """terminal_step='deleted' -> copied+1, verified+1, deleted+1, completed+1, agent:<id>:completed+1."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent.id)
    body = _make_progress_body(batch_id=batch_id, agent_id=agent.id, terminal_step="deleted")

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["copied"] == "1"
    assert h["verified"] == "1"
    assert h["deleted"] == "1"
    assert h["completed"] == "1"
    assert h["failed"] == "0"
    assert h[f"agent:{agent.id}:completed"] == "1"
    assert h[f"agent:{agent.id}:failed"] == "0"


@pytest.mark.integration
async def test_counter_math_terminal_step_verified(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """terminal_step='verified' -> copied+1, verified+1 (no deleted/completed bump)."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent.id)
    body = _make_progress_body(batch_id=batch_id, agent_id=agent.id, terminal_step="verified")

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["copied"] == "1"
    assert h["verified"] == "1"
    assert h["deleted"] == "0"
    assert h["completed"] == "0"
    assert h["failed"] == "0"
    assert h[f"agent:{agent.id}:completed"] == "0"


@pytest.mark.integration
async def test_counter_math_terminal_step_copied(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """terminal_step='copied' -> copied+1 only."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent.id)
    body = _make_progress_body(batch_id=batch_id, agent_id=agent.id, terminal_step="copied")

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["copied"] == "1"
    assert h["verified"] == "0"
    assert h["deleted"] == "0"
    assert h["completed"] == "0"
    assert h["failed"] == "0"


@pytest.mark.integration
async def test_counter_math_terminal_step_failed_at_copy(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """terminal_step='failed', failed_at_step='copy' -> failed+1, agent:<id>:failed+1 (no copied/verified)."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent.id)
    body = _make_progress_body(
        batch_id=batch_id,
        agent_id=agent.id,
        terminal_step="failed",
        failed_at_step="copy",
    )

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["failed"] == "1"
    assert h[f"agent:{agent.id}:failed"] == "1"
    assert h["copied"] == "0"
    assert h["verified"] == "0"
    assert h["deleted"] == "0"
    assert h["completed"] == "0"


@pytest.mark.integration
async def test_counter_math_terminal_step_failed_at_verify(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """terminal_step='failed', failed_at_step='verify' -> failed+1, agent:<id>:failed+1, copied+1."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent.id)
    body = _make_progress_body(
        batch_id=batch_id,
        agent_id=agent.id,
        terminal_step="failed",
        failed_at_step="verify",
    )

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["failed"] == "1"
    assert h[f"agent:{agent.id}:failed"] == "1"
    assert h["copied"] == "1"
    assert h["verified"] == "0"


@pytest.mark.integration
async def test_counter_math_terminal_step_failed_at_delete(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """terminal_step='failed', failed_at_step='delete' -> failed+1, agent:<id>:failed+1, copied+1, verified+1."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent.id)
    body = _make_progress_body(
        batch_id=batch_id,
        agent_id=agent.id,
        terminal_step="failed",
        failed_at_step="delete",
    )

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["failed"] == "1"
    assert h[f"agent:{agent.id}:failed"] == "1"
    assert h["copied"] == "1"
    assert h["verified"] == "1"
    assert h["deleted"] == "0"


# ---------------------------------------------------------------------------
# 28-V-16: sub_batch_terminal promotes status to complete / complete_with_errors
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_sub_batch_terminal_promotes_status_complete(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """sub_batch_terminal=true with subjobs_completed reaching subjobs_expected (failed==0) -> status=complete."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    # Pre-seed: subjobs_expected=1, subjobs_completed=0; the incoming POST is the 1st (and only) terminal.
    await _seed_exec_hash(redis_client, batch_id, agent.id, subjobs_expected=1, subjobs_completed=0)
    body = _make_progress_body(
        batch_id=batch_id,
        agent_id=agent.id,
        terminal_step="deleted",
        sub_batch_terminal=True,
    )

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["status"] == "complete"
    assert h["subjobs_completed"] == "1"


@pytest.mark.integration
async def test_sub_batch_terminal_promotes_status_complete_with_errors(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """sub_batch_terminal=true with failed>0 -> status=complete_with_errors."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    # Pre-seed with one failure already on the books so the terminal POST observes failed > 0.
    await _seed_exec_hash(
        redis_client,
        batch_id,
        agent.id,
        subjobs_expected=1,
        subjobs_completed=0,
        failed=2,
    )
    body = _make_progress_body(
        batch_id=batch_id,
        agent_id=agent.id,
        terminal_step="deleted",
        sub_batch_terminal=True,
    )

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["status"] == "complete_with_errors"
    assert h["subjobs_completed"] == "1"


@pytest.mark.integration
async def test_sub_batch_terminal_does_not_promote_when_not_last_subjob(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """sub_batch_terminal=true but subjobs_completed < subjobs_expected post-increment -> status unchanged."""
    agent, raw_token = seed_test_agent
    batch_id = uuid.uuid4()
    # subjobs_expected=2 so post-increment subjobs_completed=1 < 2.
    await _seed_exec_hash(redis_client, batch_id, agent.id, subjobs_expected=2, subjobs_completed=0)
    body = _make_progress_body(
        batch_id=batch_id,
        agent_id=agent.id,
        terminal_step="deleted",
        sub_batch_terminal=True,
    )

    async with _make_client(session, redis_client, raw_token) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 200, r.text
    h = await redis_client.hgetall(f"exec:{batch_id}")
    assert h["status"] == "running"
    assert h["subjobs_completed"] == "1"


# ---------------------------------------------------------------------------
# Issue #61: concurrent terminal sub-jobs must keep status consistent with failed
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_sub_batch_terminals_keep_status_consistent_with_failed(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Issue #61: >=3 concurrent terminal POSTs (one failed) leave status consistent with the failed count.

    Drives 3 sub-jobs (2 succeed, 1 fails), all ``sub_batch_terminal=True``,
    against a real Redis via ``asyncio.gather`` (the issue's acceptance scenario).
    With ``subjobs_expected=3`` the batch becomes terminal when the 3rd
    ``subjobs_completed`` lands; the final ``status`` MUST be
    ``complete_with_errors`` (consistent with ``failed==1``), never ``complete``.

    Stage 6 promotes ``status`` via a single atomic Lua script (issue #61) so the
    read of (subjobs_completed, subjobs_expected, failed) and the conditional HSET
    cannot interleave with another connection. Driven over many rounds with fresh
    batch ids to surface any concurrency regression.
    """
    agent, raw_token = seed_test_agent
    url_tmpl = "/api/internal/agent/exec-batches/{}/progress"

    async with _make_client(session, redis_client, raw_token) as ac:
        for round_idx in range(25):
            batch_id = uuid.uuid4()
            await _seed_exec_hash(
                redis_client,
                batch_id,
                agent.id,
                subjobs_expected=3,
                subjobs_completed=0,
            )
            bodies = [
                _make_progress_body(
                    batch_id=batch_id,
                    agent_id=agent.id,
                    terminal_step="deleted",
                    sub_batch_terminal=True,
                    sub_batch_index=0,
                ),
                _make_progress_body(
                    batch_id=batch_id,
                    agent_id=agent.id,
                    terminal_step="deleted",
                    sub_batch_terminal=True,
                    sub_batch_index=1,
                ),
                _make_progress_body(
                    batch_id=batch_id,
                    agent_id=agent.id,
                    terminal_step="failed",
                    failed_at_step="copy",
                    sub_batch_terminal=True,
                    sub_batch_index=2,
                ),
            ]
            url = url_tmpl.format(batch_id)
            responses = await asyncio.gather(*(ac.post(url, json=b) for b in bodies))
            assert all(r.status_code == 200 for r in responses), [r.text for r in responses]

            h = await redis_client.hgetall(f"exec:{batch_id}")
            assert h["subjobs_completed"] == "3", f"round {round_idx}: {h}"
            assert h["failed"] == "1", f"round {round_idx}: {h}"
            assert h["completed"] == "2", f"round {round_idx}: {h}"
            # The invariant: a promoted status MUST agree with the failed count.
            assert h["status"] == "complete_with_errors", (
                f"round {round_idx}: status={h['status']!r} with failed={h['failed']!r} "
                f"-- a stale-read promotion slipped through (issue #61 regression)"
            )


# ---------------------------------------------------------------------------
# Cross-tenant: explicit two-agent variant matching test_agent_scan_batches T-27-01 idiom
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_cross_tenant_403_with_two_agents(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    redis_client: redis_async.Redis,
) -> None:
    """Agent B authenticated, body says agent_id=A -> 403."""
    agent_a, _ = seed_test_agent
    # Seed a second agent inline (mirrors test_agent_scan_batches.py pattern).
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

    batch_id = uuid.uuid4()
    await _seed_exec_hash(redis_client, batch_id, agent_a.id)

    # Agent B (authenticated) posts with body.agent_id = agent_a.id.
    body = _make_progress_body(batch_id=batch_id, agent_id=agent_a.id)
    async with _make_client(session, redis_client, raw_token_b) as ac:
        r = await ac.post(f"/api/internal/agent/exec-batches/{batch_id}/progress", json=body)

    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Wiring assertion (mirrors test_agent_scan_batches.test_router_registered_in_main_app)
# ---------------------------------------------------------------------------


def test_router_registered_in_main_app() -> None:
    """Plan 28-02 Part D: phaze.main.create_app() must include the agent_exec_batches router."""
    from phaze.main import create_app
    from tests._route_introspection import iter_effective_routes

    app = create_app()
    routes = list(iter_effective_routes(app))
    paths = [r.path for r in routes]
    assert any("/api/internal/agent/exec-batches" in p for p in paths), f"agent_exec_batches.router not registered in create_app(); paths={paths}"
    matching = [r for r in routes if "/api/internal/agent/exec-batches" in r.path]
    assert any("POST" in getattr(r, "methods", set()) for r in matching), "No POST method bound on the exec-batches route"


def test_compute_increments_is_pure_function_unit() -> None:
    """The pure helper `_compute_increments` is unit-testable without Redis (verification §3)."""
    from phaze.routers.agent_exec_batches import _compute_increments
    from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload

    def _body(terminal_step: str, failed_at_step: str | None = None) -> ExecBatchProgressPayload:
        kwargs: dict[str, object] = {
            "request_id": uuid.uuid4(),
            "batch_id": uuid.uuid4(),
            "agent_id": "fileserver-x",
            "sub_batch_index": 0,
            "proposal_id": uuid.uuid4(),
            "terminal_step": terminal_step,
        }
        if failed_at_step is not None:
            kwargs["failed_at_step"] = failed_at_step
        return ExecBatchProgressPayload(**kwargs)  # type: ignore[arg-type]

    # deleted -> 5 fields
    inc = _compute_increments(_body("deleted"))
    assert inc == {
        "copied": 1,
        "verified": 1,
        "deleted": 1,
        "completed": 1,
        "agent:fileserver-x:completed": 1,
    }

    # verified -> 2 fields
    assert _compute_increments(_body("verified")) == {"copied": 1, "verified": 1}

    # copied -> 1 field
    assert _compute_increments(_body("copied")) == {"copied": 1}

    # failed at copy -> failed + agent:failed
    assert _compute_increments(_body("failed", "copy")) == {
        "failed": 1,
        "agent:fileserver-x:failed": 1,
    }

    # failed at verify -> +copied
    assert _compute_increments(_body("failed", "verify")) == {
        "failed": 1,
        "agent:fileserver-x:failed": 1,
        "copied": 1,
    }

    # failed at delete -> +copied +verified
    assert _compute_increments(_body("failed", "delete")) == {
        "failed": 1,
        "agent:fileserver-x:failed": 1,
        "copied": 1,
        "verified": 1,
    }
