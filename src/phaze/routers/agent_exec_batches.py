"""POST /api/internal/agent/exec-batches/{batch_id}/progress -- per-proposal terminal-state event (Phase 28 D-05, D-17).

Handler ordering (the ORDER is part of the contract, per T-28-02-S1/I1):
  1. 403 if ``body.agent_id != agent.id`` -- cross-tenant guard BEFORE any
     state read (mirrors Phase 26 D-08 timing-side-channel pattern; a leaked
     ``batch_id`` cannot be probed via 200 vs 404 timing).
  2. 404 if ``exec:{batch_id}`` hash doesn't exist (HEXISTS on the ``total``
     field). Unknown and expired batches return the same opaque
     ``"batch not found"`` detail (no oracle for the operator's batch
     lifecycle).
  3. 403 if ``agent:<body.agent_id>:total`` rollup field is absent -- the
     per-agent rollup is pre-set at dispatch time (D-09 step 5), so its
     absence is structural proof the caller wasn't part of this dispatch
     (D-17 step 4).
  4. SET NX EX dedup on ``exec_progress_req:{request_id}`` -- duplicate
     returns 200 with NO HINCRBY (Stripe-style idempotency; D-15).
  5. HINCRBY counters per the D-07 rules (computed by ``_compute_increments``;
     pipelined for one network round-trip).
  6. If ``sub_batch_terminal`` is True, HINCRBY ``subjobs_completed`` and
     promote ``status`` to ``"complete"`` / ``"complete_with_errors"`` when
     ``subjobs_completed == subjobs_expected`` (D-07 final clause).

This module deliberately omits ``from __future__ import annotations`` so
FastAPI can resolve ``Annotated[redis_async.Redis, Depends(_get_redis)]`` at
app-build time (matches agent_tracklists.py / agent_scan_batches.py).

Decisions implemented: D-02 (app server owns exec:{batch_id} writes
exclusively; agents never write Redis directly), D-05 (endpoint shape +
prefix), D-06 (request schema), D-07 (counter math), D-15 (Stripe-style
request-id idempotency), D-17 (4-stage cross-tenant guard).
"""

from typing import TYPE_CHECKING, Annotated, cast
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
import redis.asyncio as redis_async

from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload


if TYPE_CHECKING:
    # `Awaitable` is referenced only inside string-quoted ``cast(...)`` calls
    # below to satisfy mypy on the redis-py `Awaitable[T] | T` overloaded
    # async return types; it is never used at runtime.
    from collections.abc import Awaitable


router = APIRouter(prefix="/api/internal/agent/exec-batches", tags=["agent-internal"])


_REQ_PREFIX = "exec_progress_req:"
_TTL_SECONDS = 3600  # 1-hour idempotency window (D-15)


async def _get_redis(request: Request) -> redis_async.Redis:
    """Pull the Redis client from ``app.state`` (decode_responses=True per main.py).

    NOT ``app.state.queue.redis`` -- the SAQ-internal client has
    ``decode_responses=False``. The shared client wired in ``main.lifespan``
    (Phase 26 D-27) is the right handle so ``.hget``/``.hgetall`` return ``str``.
    """
    redis_client: redis_async.Redis = request.app.state.redis
    return redis_client


def _compute_increments(body: ExecBatchProgressPayload) -> dict[str, int]:
    """D-07 counter update rules. Returns the HINCRBY dict for this progress event.

    The agent reports the TERMINAL step it actually reached -- the controller
    fills in the "implied prior steps" so the global counters
    (``copied`` / ``verified`` / ``deleted``) always correspond to the count
    of proposals that actually completed THAT step. This mirrors the
    D-03 trade-off (one POST per file, server fills in the step ladder).

    Caller invariant: ``body`` has already been validated by Pydantic, so
    ``terminal_step == "failed"`` implies ``failed_at_step is not None``.
    """
    agent_id = body.agent_id
    if body.terminal_step == "deleted":
        return {
            "copied": 1,
            "verified": 1,
            "deleted": 1,
            "completed": 1,
            f"agent:{agent_id}:completed": 1,
        }
    if body.terminal_step == "verified":
        return {"copied": 1, "verified": 1}
    if body.terminal_step == "copied":
        return {"copied": 1}
    # terminal_step == "failed" -- failed_at_step is guaranteed non-null by the schema.
    inc: dict[str, int] = {"failed": 1, f"agent:{agent_id}:failed": 1}
    if body.failed_at_step == "verify":
        inc["copied"] = 1
    elif body.failed_at_step == "delete":
        inc["copied"] = 1
        inc["verified"] = 1
    return inc


@router.post("/{batch_id}/progress", status_code=status.HTTP_200_OK)
async def post_exec_batch_progress(
    batch_id: uuid.UUID,
    body: ExecBatchProgressPayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    redis_client: Annotated[redis_async.Redis, Depends(_get_redis)],
) -> Response:
    """Per-proposal terminal-state event handler (D-05, D-07, D-15, D-17).

    Returns:
        Response: 200 with no body. The aggregate state is read via SSE on
        ``GET /execution/progress/{batch_id}`` -- there is no response data
        the agent needs from this call.

    Raises:
        HTTPException(401): no bearer token (from the auth dep).
        HTTPException(403): ``body.agent_id != agent.id`` (cross-tenant
            spoofing attempt) OR the per-agent rollup is absent (caller
            wasn't part of this dispatch).
        HTTPException(404): ``exec:{batch_id}`` hash is missing (unknown or
            expired batch -- same opaque detail per D-17 step 3).

    Security:
        - ``agent`` is bound from the auth dep, NEVER from the body (AUTH-01).
        - The 4-stage validation is ORDERED: cross-tenant 403 fires BEFORE
          any HEXISTS read so a forged ``agent_id`` cannot leak whether a
          ``batch_id`` exists via 404-vs-403 timing.
        - Idempotency via SET NX EX 3600 on ``exec_progress_req:{request_id}``
          makes the endpoint safe for SAQ-retry replays (D-15).
    """
    # ---- Stage 1: cross-tenant guard. Runs BEFORE any Redis state read
    # (D-17 step 2 / T-28-02-S1 / T-28-02-I1). A leaked batch_id paired
    # with a stolen-or-misconfigured bearer must still produce 403, never
    # a 404 that could be used to map the batch space.
    if body.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent_id in body does not match authenticated agent",
        )

    key = f"exec:{batch_id}"

    # ---- Stage 2: 404 if the batch hash doesn't exist. Single opaque detail
    # (D-17 step 3) -- unknown and expired batches look the same.
    # `redis_async.Redis.hexists` is typed `Awaitable[bool] | bool` because the
    # redis-py stubs share between sync and async APIs; cast to the awaitable
    # variant for mypy in this async handler.
    if not await cast("Awaitable[bool]", redis_client.hexists(key, "total")):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="batch not found",
        )

    # ---- Stage 3: D-17 step 4 -- the per-agent rollup field is pre-set at
    # dispatch (D-09 step 5) so its absence is structural proof this agent
    # wasn't part of the dispatch. Reject 403 BEFORE any HINCRBY so we
    # never silently create an unauthorized rollup field.
    if not await cast("Awaitable[bool]", redis_client.hexists(key, f"agent:{body.agent_id}:total")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent was not part of this dispatch",
        )

    # ---- Stage 4: SET NX EX dedup. Duplicate POST (same request_id within
    # the 1-hour window) returns 200 with NO HINCRBY (D-15). Replays from
    # SAQ retries are safe.
    req_key = f"{_REQ_PREFIX}{body.request_id}"
    won = await redis_client.set(req_key, "1", nx=True, ex=_TTL_SECONDS)
    if not won:
        return Response(status_code=status.HTTP_200_OK)

    # ---- Stage 5: HINCRBY the D-07 counter set. Pipelined so all
    # increments + the optional sub_batch_terminal increment hit Redis in
    # one round-trip (transaction=False -- HINCRBY on disjoint fields is
    # commutative; no MULTI/EXEC needed). The `pipe.hincrby` chained calls
    # return the pipeline itself (Awaitable in async mode); await is a
    # noop-friendly wrapper that the redis-py stubs require.
    increments = _compute_increments(body)
    async with redis_client.pipeline(transaction=False) as pipe:
        for field, by in increments.items():
            await cast("Awaitable[int]", pipe.hincrby(key, field, by))
        if body.sub_batch_terminal:
            await cast("Awaitable[int]", pipe.hincrby(key, "subjobs_completed", 1))
        await pipe.execute()

    # ---- Stage 6: terminal-status detection. ONLY fires when the agent
    # marks this as its last proposal in the sub-batch -- avoids polling
    # the equality check on every progress POST (D-07 final clause).
    if body.sub_batch_terminal:
        sc = int(await cast("Awaitable[str | None]", redis_client.hget(key, "subjobs_completed")) or 0)
        se = int(await cast("Awaitable[str | None]", redis_client.hget(key, "subjobs_expected")) or 0)
        if sc == se:
            failed = int(await cast("Awaitable[str | None]", redis_client.hget(key, "failed")) or 0)
            new_status = "complete" if failed == 0 else "complete_with_errors"
            await cast("Awaitable[int]", redis_client.hset(key, "status", new_status))

    return Response(status_code=status.HTTP_200_OK)
