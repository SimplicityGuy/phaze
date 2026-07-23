"""POST /api/internal/agent/exec-batches/{batch_id}/progress -- per-proposal terminal-state event (Phase 28 D-05, D-17).

Handler ordering (the ORDER is part of the contract, per T-28-02-S1/I1). Stages 1-3 are the
D-17 cross-tenant SECURITY guards and their order is FIXED; phaze-gtau reworked only the
token-vs-work ordering below them (stages 4-6):
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
  4/5. ATOMIC dedup + D-07 counters + request marker (phaze-gtau). ONE Lua
     script (``_APPLY_INCREMENTS_LUA``) checks the ``exec_progress_req:{request_id}``
     idempotency marker, applies the D-07 HINCRBY set (computed by
     ``_compute_increments``, plus ``subjobs_completed`` when terminal), and SETs
     the marker -- all in one round-trip. The marker becomes authoritative ONLY
     together with the counters, so a crash mid-span can never burn the marker with
     the increments unapplied (the old ``SET NX marker`` BEFORE the HINCRBY pipeline
     silently lost them on the retry). A duplicate request_id applies nothing
     (Stripe-style idempotency; D-15); a reaped batch applies nothing (phaze-pyv3).
  6. If ``sub_batch_terminal`` is True, promote ``status`` to ``"complete"`` /
     ``"complete_with_errors"`` when ``subjobs_completed == subjobs_expected`` (D-07
     final clause). Runs even on a deduped replay so a crash between the atomic
     apply and the promotion cannot strand a terminal batch at ``"running"`` on retry
     (the promotion is idempotent).

This module deliberately omits ``from __future__ import annotations`` so
FastAPI can resolve ``Annotated[redis_async.Redis, Depends(_get_redis)]`` at
app-build time (matches agent_tracklists.py / agent_scan_batches.py).

Decisions implemented: D-02 (app server owns exec:{batch_id} writes
exclusively; agents never write Redis directly), D-05 (endpoint shape +
prefix), D-06 (request schema), D-07 (counter math), D-15 (Stripe-style
request-id idempotency), D-17 (4-stage cross-tenant guard).
"""

from typing import TYPE_CHECKING, Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
import redis.asyncio as redis_async

from phaze.models.agent import Agent
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload


if TYPE_CHECKING:
    from redis.commands.core import AsyncScript


router = APIRouter(prefix="/api/internal/agent/exec-batches", tags=["agent-internal"])


_REQ_PREFIX = "exec_progress_req:"
_TTL_SECONDS = 3600  # 1-hour idempotency window (D-15)

# phaze-fa2p: the single-dispatch sentinel. ``routers/execution.start_execution`` claims this key
# with ``SET NX`` before seeding/enqueuing a batch so a concurrent or repeated POST cannot
# double-dispatch the same still-APPROVED proposals. It is released atomically with the terminal
# status promotion below (and by a 24h safety TTL if a batch's sub-jobs never report terminal).
ACTIVE_DISPATCH_KEY = "exec:active"


# Lua: atomically read the sub-batch counters and promote `status` in a SINGLE
# round-trip (issue #61). The prior three-HGET-then-conditional-HSET sequence
# had a window where a concurrent terminal POST could observe
# ``subjobs_completed == subjobs_expected`` while another sub-job's ``failed``
# HINCRBY had not yet landed, then HSET ``status="complete"`` over a batch that
# actually had a failure -- the operator's SSE close-event would say "complete"
# with ``failed >= 1``. Redis executes the script atomically, so the read of
# (subjobs_completed, subjobs_expected, failed) and the conditional HSET cannot
# interleave with any other connection. Mirrors the D-04 / D-07 read-then-write
# semantics exactly (only the atomicity is added). Returns 1 if status was
# promoted, 0 otherwise; the caller does not use the result.
#
# phaze-fa2p: when the batch reaches a terminal status this is also the single atomic point that
# releases the ``exec:active`` single-dispatch sentinel -- but ONLY when it still names THIS batch
# (``GET == ARGV[1]``), so a newer dispatch that already re-claimed the sentinel is never cleared.
# ``KEYS[2]``/``ARGV[1]`` are optional: a caller that passes only ``keys=[key]`` (numkeys=1) leaves
# ``KEYS[2]`` nil and the release block is skipped, keeping the script backward compatible.
_PROMOTE_STATUS_LUA = """
local key = KEYS[1]
if redis.call('EXISTS', key) == 0 then return 0 end
local sc = tonumber(redis.call('HGET', key, 'subjobs_completed') or '0')
local se = tonumber(redis.call('HGET', key, 'subjobs_expected') or '0')
if sc ~= se then return 0 end
local failed = tonumber(redis.call('HGET', key, 'failed') or '0')
local new_status = (failed == 0) and 'complete' or 'complete_with_errors'
redis.call('HSET', key, 'status', new_status)
local active_key = KEYS[2]
local batch_id = ARGV[1]
if active_key and active_key ~= '' and batch_id and redis.call('GET', active_key) == batch_id then
  redis.call('DEL', active_key)
end
return 1
"""

# redis-py computes the script SHA when ``register_script`` is called. There is
# no Redis handle at import time, so register lazily on first use and cache the
# AsyncScript so subsequent terminal POSTs reuse the EVALSHA fast-path. The live
# client is passed at call time, so the cached script survives client recycling.
_promote_status_script: "AsyncScript | None" = None


def _get_promote_status_script(redis_client: redis_async.Redis) -> "AsyncScript":
    """Return the cached status-promotion script, registering it on first call."""
    global _promote_status_script
    if _promote_status_script is None:
        _promote_status_script = redis_client.register_script(_PROMOTE_STATUS_LUA)
    return _promote_status_script


# phaze-gtau: apply the D-07 HINCRBY counter set AND claim the request-idempotency marker
# (KEYS[2]) ATOMICALLY, in one round-trip. The marker becomes authoritative ONLY together with the
# HINCRBYs, closing the window the prior "SET NX marker, THEN pipeline HINCRBY" ordering left open:
# a crash between the marker set and the increments burned the marker with the counters unapplied,
# so the tenacity/SAQ retry (identical request_id) short-circuited on the marker into a clean 200
# and the increments were LOST forever (a lost terminal event stranded the batch at 'running' until
# its 24h TTL). Here either BOTH the increments and the marker land, or NEITHER; a duplicate
# request (marker already present) applies nothing.
#
# phaze-pyv3 (PRESERVED): the EXISTS(KEYS[1]) guard keeps a batch reaped by its 24h TTL between the
# stage 2/3 HEXISTS checks and here from being RESURRECTED by a bare HINCRBY (which would leak a
# TTL-less, status-less phantom hash forever). The apply is a no-op when the hash is already gone,
# and the marker is NOT claimed for a dead batch (nothing to protect against a replay of).
#
# KEYS[1] = exec:{batch_id}; KEYS[2] = exec_progress_req:{request_id}. ARGV[1] = marker TTL seconds;
# ARGV[2..] is a flat [field, by, field, by, ...] list; the caller appends ('subjobs_completed', 1)
# when the sub-batch is terminal. Returns 1 if applied+claimed, 0 if the hash was already gone
# (pyv3), -1 if the request was a duplicate (marker already present -> nothing applied).
_APPLY_INCREMENTS_LUA = """
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local i = 2
while i < #ARGV do
  redis.call('HINCRBY', KEYS[1], ARGV[i], tonumber(ARGV[i + 1]))
  i = i + 2
end
redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[1]))
return 1
"""
_apply_increments_script: "AsyncScript | None" = None


def _get_apply_increments_script(redis_client: redis_async.Redis) -> "AsyncScript":
    """Return the cached atomic dedup+HINCRBY+marker script, registering it on first call (phaze-gtau)."""
    global _apply_increments_script
    if _apply_increments_script is None:
        _apply_increments_script = redis_client.register_script(_APPLY_INCREMENTS_LUA)
    return _apply_increments_script


async def _get_redis(request: Request) -> redis_async.Redis:
    """Pull the Redis client from ``app.state`` (decode_responses=True per main.py).

    NOT ``app.state.controller_queue.redis`` -- the SAQ-internal client has
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
        - The D-17 validation is ORDERED: cross-tenant 403 fires BEFORE
          any HEXISTS read so a forged ``agent_id`` cannot leak whether a
          ``batch_id`` exists via 404-vs-403 timing (stages 1-3, UNCHANGED).
        - Idempotency via the ``exec_progress_req:{request_id}`` marker, set
          ATOMICALLY with the counters (phaze-gtau), makes the endpoint safe
          for SAQ-retry replays (D-15) without a mid-span crash losing them.
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
    if not await redis_client.hexists(key, "total"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="batch not found",
        )

    # ---- Stage 3: D-17 step 4 -- the per-agent rollup field is pre-set at
    # dispatch (D-09 step 5) so its absence is structural proof this agent
    # wasn't part of the dispatch. Reject 403 BEFORE any HINCRBY so we
    # never silently create an unauthorized rollup field.
    if not await redis_client.hexists(key, f"agent:{body.agent_id}:total"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent was not part of this dispatch",
        )

    # ---- Stage 4+5 (phaze-gtau): dedup + D-07 counters + the request-idempotency marker, applied
    # as ONE atomic Lua (``_APPLY_INCREMENTS_LUA``). The marker becomes authoritative ONLY together
    # with the HINCRBYs, so a crash mid-span can never leave the marker set with the increments
    # unapplied. The OLD ordering set the marker (SET NX) FIRST and only THEN ran the HINCRBY
    # pipeline: a crash / Redis timeout / pod eviction in that gap durably burned the marker, and the
    # agent's tenacity/SAQ retry (identical request_id, persisted in the job meta) short-circuited on
    # the marker into a clean 200 with the counters LOST forever -- a lost terminal event stranded the
    # batch at 'running' until the 24h TTL dropped the hash. Now either both land or neither does; a
    # duplicate request_id applies nothing (D-15 dedup), and a batch reaped by its 24h TTL between the
    # stage 2/3 HEXISTS checks and here applies nothing and is never resurrected (phaze-pyv3). Note
    # the D-17 stages 1-3 above are UNCHANGED -- only this token-vs-work ordering moved. ARGV is
    # [ttl, field, by, ...] with the optional sub_batch_terminal ('subjobs_completed', 1) appended.
    req_key = f"{_REQ_PREFIX}{body.request_id}"
    increments = _compute_increments(body)
    apply_args: list[str] = [str(_TTL_SECONDS)]
    for field, by in increments.items():
        apply_args.extend((field, str(by)))
    if body.sub_batch_terminal:
        apply_args.extend(("subjobs_completed", "1"))
    apply_increments = _get_apply_increments_script(redis_client)
    await apply_increments(keys=[key, req_key], args=apply_args, client=redis_client)

    # ---- Stage 6: terminal-status detection + promotion (D-07 final clause).
    # Fires whenever the agent marks this as its last proposal in the sub-batch
    # -- INCLUDING on a duplicate replay whose increments were deduped above.
    # phaze-gtau: promoting on the deduped path is REQUIRED, not wasteful -- it
    # covers the crash window between the atomic apply+marker (stage 4+5) and this
    # promotion. Were it skipped once the marker is present, a crash there would
    # leave the terminal ``subjobs_completed`` applied but the status never
    # promoted, stranding the batch at 'running' forever on retry (the terminal-loss
    # half of the defect). The promotion is idempotent: HSET status is a set and the
    # ``exec:active`` release is GET==batch_id-guarded (phaze-fa2p), so re-running it
    # on a true duplicate is a harmless no-op. The read-then-write is delegated to a
    # Lua script so it executes atomically on the Redis server; under >=3 concurrent
    # terminal sub-jobs this is what prevents a stale `failed` read from promoting a
    # failed batch to "complete" (issue #61).
    if body.sub_batch_terminal:
        promote_status = _get_promote_status_script(redis_client)
        # phaze-fa2p: pass the sentinel key + this batch_id so a terminal promotion also releases
        # the single-dispatch claim atomically (see ACTIVE_DISPATCH_KEY / _PROMOTE_STATUS_LUA).
        await promote_status(keys=[key, ACTIVE_DISPATCH_KEY], args=[str(batch_id)], client=redis_client)

    return Response(status_code=status.HTTP_200_OK)
