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
     ``"complete_with_errors"`` once ``subjobs_completed >= subjobs_expected`` (D-07
     final clause). Runs even on a deduped replay so a crash between the atomic
     apply and the promotion cannot strand a terminal batch at ``"running"`` on retry
     (the promotion is idempotent). phaze-a6t8 folded this stage INTO the stage 4/5
     script -- apply and promote are one atomic span, so no concurrent sub-job can
     interleave its increment between them -- and relaxed the predicate from exact
     equality to a ``>=`` threshold, so an overshoot promotes instead of wedging the
     batch (and the ``exec:active`` sentinel) until the 24h TTL.

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

# phaze-a6t8: the D-15 dedup window MUST cover the whole life of the batch it protects, not a
# shorter slice of it. This was 3600 (1h) while ``exec:{batch_id}`` lives 86400 (24h) and the
# agent's ``request_id`` is persisted in the SAQ job ``meta`` precisely so an arbitrarily-delayed
# requeue replays with the SAME id. A replay landing more than an hour after the original found
# ``EXISTS(marker) == 0`` and re-applied the FULL increment set -- including the terminal
# ``subjobs_completed`` -- so the D-15 contract silently lapsed exactly when it was needed most
# (a long backoff / evicted-pod requeue is the only way a replay happens at all). Pinned to the
# batch hash's own TTL so the marker cannot outlive, or under-live, the counters it guards.
_TTL_SECONDS = 86400  # 24-hour idempotency window -- matches the exec:{batch_id} hash TTL (D-15)

# phaze-fa2p/phaze-c3j0: the single-dispatch sentinel prevents concurrent dispatch of the same
# approved proposals and has a distinct namespace from per-batch hashes. Terminal promotion
# releases it atomically; its safety TTL releases a batch whose sub-jobs never report terminal.
ACTIVE_DISPATCH_KEY = "execdispatch:active"

# The per-batch progress hash key prefix (``exec:{batch_id}``), named once so the claim-reconcile
# Lua can rebuild a held batch's key from the sentinel's value without hard-coding the spelling.
BATCH_KEY_PREFIX = "exec:"

# Safety TTL on the single-dispatch claim. The designed worst case: if a batch's sub-jobs never
# report terminal AND the claim-time reconcile below cannot prove the batch dead, the sentinel
# self-expires rather than wedging the execute stage forever.
DISPATCH_CLAIM_TTL_SECONDS = 86400


# D-04/D-07: promotion reads completion and failure counters atomically so SSE cannot report
# ``complete`` from a stale failure count (issue #61). phaze-a6t8 uses the terminal threshold
# ``subjobs_completed >= subjobs_expected``; overshoot is still terminal. phaze-fa2p releases the
# sentinel only when it still names this batch. Both Lua entry points share this function so their
# promotion predicate cannot drift.
_PROMOTE_LUA_FN = """
local function phaze_promote(key, active_key, batch_id)
  if redis.call('EXISTS', key) == 0 then return 0 end
  local sc = tonumber(redis.call('HGET', key, 'subjobs_completed') or '0')
  local se = tonumber(redis.call('HGET', key, 'subjobs_expected') or '0')
  if sc < se then return 0 end
  local failed = tonumber(redis.call('HGET', key, 'failed') or '0')
  local new_status = (failed == 0) and 'complete' or 'complete_with_errors'
  redis.call('HSET', key, 'status', new_status)
  if active_key and active_key ~= '' and batch_id and redis.call('GET', active_key) == batch_id then
    redis.call('DEL', active_key)
  end
  return 1
end
"""

_PROMOTE_STATUS_LUA = (
    _PROMOTE_LUA_FN
    + """
return phaze_promote(KEYS[1], KEYS[2], ARGV[1])
"""
)

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


# phaze-j7u8: claiming also reconciles a sentinel whose batch hash is gone, already terminal, or
# complete by counters but not yet promoted. Any live under-count remains in flight and fails
# closed: refusing an operator click is safer than dispatching the same archive move twice. Redis
# cannot distinguish a lost terminal token from a slow copy, so ambiguous claims retain the 24h TTL.
#
# KEYS[1] = the sentinel key. ARGV[1] = this dispatch's batch_id, ARGV[2] = claim TTL seconds,
# ARGV[3] = the per-batch hash key prefix. Returns 1 if claimed outright, 2 if claimed by
# reconciling a stale sentinel (caller should log it -- it means an earlier dispatch leaked), and
# 0 if refused because a dispatch is genuinely in flight.
_CLAIM_DISPATCH_LUA = (
    _PROMOTE_LUA_FN
    + """
local held = redis.call('GET', KEYS[1])
if not held then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
  return 1
end
local held_key = ARGV[3] .. held
if redis.call('EXISTS', held_key) == 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
  return 2
end
local st = redis.call('HGET', held_key, 'status')
if st == 'complete' or st == 'complete_with_errors' then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
  return 2
end
local sc = tonumber(redis.call('HGET', held_key, 'subjobs_completed') or '0')
local se = tonumber(redis.call('HGET', held_key, 'subjobs_expected') or '0')
if sc >= se then
  phaze_promote(held_key, KEYS[1], held)
  redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
  return 2
end
return 0
"""
)
_claim_dispatch_script: "AsyncScript | None" = None


def _get_claim_dispatch_script(redis_client: redis_async.Redis) -> "AsyncScript":
    """Return the cached claim-or-reconcile script, registering it on first call (phaze-j7u8)."""
    global _claim_dispatch_script
    if _claim_dispatch_script is None:
        _claim_dispatch_script = redis_client.register_script(_CLAIM_DISPATCH_LUA)
    return _claim_dispatch_script


# phaze-0t2c: compare-and-delete release for a dispatch that ends before any sub-job lands (so no
# terminal POST will ever run the promotion that normally releases the claim). The GET==batch_id
# guard is the whole point and mirrors the promote script's: on the failure path this can run well
# after the claim was taken, and an unconditional DEL would drop a NEWER dispatch's claim,
# re-opening the double-move hazard the sentinel exists to close.
# KEYS[1] = the sentinel key. ARGV[1] = the batch_id that must still hold it. Returns 1 if released.
_RELEASE_DISPATCH_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_release_dispatch_script: "AsyncScript | None" = None


def _get_release_dispatch_script(redis_client: redis_async.Redis) -> "AsyncScript":
    """Return the cached CAS-release script, registering it on first call (phaze-0t2c)."""
    global _release_dispatch_script
    if _release_dispatch_script is None:
        _release_dispatch_script = redis_client.register_script(_RELEASE_DISPATCH_LUA)
    return _release_dispatch_script


# phaze-gtau/phaze-a6t8: increments, request-idempotency marker, promotion, and sentinel release
# share one atomic script. A retry therefore observes either all effects or none. phaze-pyv3 keeps
# the batch-exists guard in that span so a reaped hash cannot be resurrected without a TTL/status.
#
# KEYS[1] = exec:{batch_id}; KEYS[2] = exec_progress_req:{request_id}; KEYS[3] = the exec:active
# sentinel (pass '' to skip the release). ARGV[1] = marker TTL seconds; ARGV[2] = '1' when the
# sub-batch is terminal (else ''); ARGV[3] = this batch_id (for the sentinel's GET==batch_id CAS);
# ARGV[4..] is a flat [field, by, field, by, ...] list, with ('subjobs_completed', 1) appended by
# the caller when terminal. Returns 1 if applied+claimed, 0 if the hash was already gone (pyv3),
# -1 if the request was a duplicate (marker already present -> nothing applied, but the promotion
# below STILL runs -- see stage 6's note on the deduped path).
_APPLY_INCREMENTS_LUA = (
    _PROMOTE_LUA_FN
    + """
local duplicate = redis.call('EXISTS', KEYS[2]) == 1
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if not duplicate then
  local i = 4
  while i < #ARGV do
    redis.call('HINCRBY', KEYS[1], ARGV[i], tonumber(ARGV[i + 1]))
    i = i + 2
  end
  redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[1]))
end
if ARGV[2] == '1' then
  phaze_promote(KEYS[1], KEYS[3], ARGV[3])
end
if duplicate then return -1 end
return 1
"""
)
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
    # Cross-tenant authorization runs BEFORE any Redis state read
    # (D-17 step 2 / T-28-02-S1 / T-28-02-I1). A leaked batch_id paired
    # with a stolen-or-misconfigured bearer must still produce 403, never
    # a 404 that could be used to map the batch space.
    if body.agent_id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent_id in body does not match authenticated agent",
        )

    key = f"exec:{batch_id}"

    # Unknown and expired batch hashes share one opaque 404
    # (D-17 step 3) -- unknown and expired batches look the same.
    if not await redis_client.hexists(key, "total"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="batch not found",
        )

    # D-17: the per-agent rollup field is pre-set at
    # dispatch (D-09 step 5) so its absence is structural proof this agent
    # wasn't part of the dispatch. Reject 403 BEFORE any HINCRBY so we
    # never silently create an unauthorized rollup field.
    if not await redis_client.hexists(key, f"agent:{body.agent_id}:total"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent was not part of this dispatch",
        )

    # phaze-gtau: dedup marker, D-07 counters, promotion, and sentinel release are one atomic span.
    # Duplicate requests apply nothing, and phaze-pyv3 prevents a reaped hash from being resurrected.
    req_key = f"{_REQ_PREFIX}{body.request_id}"
    increments = _compute_increments(body)
    # phaze-a6t8: ARGV is [ttl, terminal_flag, batch_id, field, by, ...] -- the terminal flag and
    # batch_id ride along so the script can run stage 6's promotion in the SAME atomic span.
    apply_args: list[str] = [str(_TTL_SECONDS), "1" if body.sub_batch_terminal else "", str(batch_id)]
    for field, by in increments.items():
        apply_args.extend((field, str(by)))
    if body.sub_batch_terminal:
        apply_args.extend(("subjobs_completed", "1"))
    apply_increments = _get_apply_increments_script(redis_client)
    # Promotion also runs on a deduped terminal replay so a prior apply cannot remain unpromoted.
    # It is idempotent and server-side atomic, preventing stale failure reads under concurrency
    # (issue #61); phaze-fa2p releases only this batch's sentinel in the same span.
    await apply_increments(keys=[key, req_key, ACTIVE_DISPATCH_KEY], args=apply_args, client=redis_client)

    return Response(status_code=status.HTTP_200_OK)
