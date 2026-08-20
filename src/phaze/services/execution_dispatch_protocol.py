"""The transport-neutral half of Execute Approved: seed -> claim -> enqueue -> reconcile.

phaze-1i0h6.5 extracted this from :func:`phaze.routers.execution.start_execution`, which had
grown to CCN 19 / 165 NLOC / 4 levels of nesting around ONE cohesive stateful protocol wrapped in
HTTP concerns. The split runs exactly where the handler stops touching the database: everything
BEFORE the explicit pre-dispatch ``session.commit()`` (collision pre-check, the grouping /
revoked-agent reads, the per-agent display-name query, ``Request``/template handling and the
response selection) stays in the route; everything AFTER it -- which is Redis and broker I/O and
nothing else -- lives here.

That seam is not cosmetic. The commit exists (phaze-266lc) so the read-only transaction the route
autobegan is released BEFORE the enqueue loop, which spans many suspension points and real wall
time on a large approved set; a service that begins after it is structurally incapable of
re-opening a transaction and holding it across that loop. Nothing in this module takes an
``AsyncSession``, and it must stay that way.

The protocol itself is unchanged, and the ORDER of its steps is the contract. Read the inline
bead references before reordering anything -- every one of them is a shipped incident:

1. **Seed, then claim** (phaze-0t2c). The ``exec:{batch_id}`` hash is written before the
   ``execdispatch:active`` sentinel is taken, never the other way round, so a Redis fault on the
   seed cannot leave a claim outstanding against a batch whose hash never existed -- an
   unreleasable sentinel that refused every Execute Approved for the next 24h.
2. **A lost claim reply drops the seeded hash** (phaze-tnp06) before re-raising, shielded.
3. **The enqueue loop settles on ANY exit**, including cancellation (phaze-0t2c), and the chunk
   that was in flight when it was interrupted is counted as *landed*, not failed (phaze-19u7g) --
   as is an :class:`AmbiguousEnqueueError`, for the same reason: lowering ``subjobs_expected``
   past a chunk that may really have committed on the broker lets the batch promote terminal and
   release the sentinel while a phantom sub-job is still moving files.
4. **Undispatched proposals are rolled into per-agent counters** (phaze-1h6j) as well as the
   batch-level one, so each affected agent's row can reach a terminal pill.

What this module deliberately does NOT own: the ``running`` / ``waiting`` semantics (untouched
here -- this seam does not repoint either field), the audit routes, and ``execution_progress``.
Dispatch logging DOES live here, D-11 INFO line included -- see :func:`_dispatched`.

The three Lua entry points arrive as :class:`DispatchScripts` rather than being imported here, so
the protocol can be driven -- and mutation-checked -- without a Redis server or an HTTP client,
and so the route stays the single place that names them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
from typing import TYPE_CHECKING, cast
import uuid

import structlog

from phaze.routers.agent_exec_batches import ACTIVE_DISPATCH_KEY, BATCH_KEY_PREFIX, DISPATCH_CLAIM_TTL_SECONDS
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.execution_dispatch import chunk_proposals


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from redis.asyncio import Redis
    from redis.commands.core import AsyncScript

    from phaze.schemas.agent_tasks import ExecuteBatchProposalItem
    from phaze.services.agent_task_router import AgentTaskRouter

    ScriptFactory = Callable[[Redis], AsyncScript]
    RedisValue = bytes | bytearray | memoryview[int] | str | int | float
    RedisFieldMapping = Mapping[RedisValue, RedisValue]
    ChunkPlan = dict[str, list[list[ExecuteBatchProposalItem]]]
    PendingChunks = list[tuple[str, int, list[ExecuteBatchProposalItem]]]


logger = structlog.get_logger(__name__)


class DispatchOutcomeKind(StrEnum):
    """Which of the four terminal shapes a dispatch attempt reached."""

    STARTED = "started"
    """The sentinel was claimed and every planned chunk is inside ``subjobs_expected``."""

    PARTIAL = "partial"
    """Claimed, but at least one chunk definitively never landed -- the reconcile ran.

    Covers the ambiguous case too: an ambiguous chunk is kept inside ``subjobs_expected``, so a
    dispatch whose ONLY failures were ambiguous reports :attr:`STARTED` (nothing to reconcile),
    while a mixed dispatch reports this.
    """

    REFUSED = "refused"
    """Another dispatch holds ``execdispatch:active``; nothing was seeded, claimed, or enqueued."""

    EMPTY = "empty"
    """No approved work: no hash, no claim, no enqueue. The batch id exists only for the render."""


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What the protocol did, in the terms the caller renders from.

    Deliberately carries no ``Response``, template name, or ``Request`` -- the route selects those.
    ``undispatched`` is the proposal count the first render surfaces as ``failed``; ``status`` is
    the render status ("running", or "complete_with_errors" when nothing landed at all).
    """

    kind: DispatchOutcomeKind
    batch_id: uuid.UUID
    total: int
    subjobs_expected: int
    undispatched: int = 0
    status: str = "running"


@dataclass(frozen=True, slots=True)
class DispatchScripts:
    """The three Lua entry points this protocol drives, resolved by the caller.

    Injected rather than imported so the protocol is drivable in isolation, and so
    ``routers/execution`` remains the one module that names the registration helpers.
    """

    claim: ScriptFactory
    """``_get_claim_dispatch_script`` -- claim-or-reconcile the single-dispatch sentinel (phaze-j7u8)."""

    release: ScriptFactory
    """``_get_release_dispatch_script`` -- CAS release, used only when nothing landed (phaze-0t2c)."""

    promote: ScriptFactory
    """``_get_promote_status_script`` -- re-run the terminal-status promotion after a reconcile."""


def _dispatched(
    kind: DispatchOutcomeKind,
    *,
    batch_id: uuid.UUID,
    total: int,
    subjobs_expected: int,
    n_agents: int,
    undispatched: int = 0,
    status: str = "running",
) -> DispatchOutcome:
    """Build a dispatched outcome AND emit the D-11 dispatch INFO line -- deliberately one step.

    Every non-refused exit builds its outcome through here, so the D-11 line cannot be forgotten at
    a return site added later, and it can only ever report the figures the caller is about to
    render. A REFUSED dispatch is the one shape that does NOT come through here: nothing was
    dispatched, so there is nothing for a dispatch line to describe.

    The logger moved with the code (phaze-1i0h6.5); this line is emitted by
    ``phaze.services.execution_dispatch_protocol``, NOT by ``phaze.routers.execution``, and any
    operator filter or log capture has to name this module. It keeps emitting because
    ``logging_config.configure_logging`` sets the ROOT level (default INFO, ``PHAZE_LOG_LEVEL``
    overrides) and every entry point calls it -- so do not "restore" this to the route's logger on
    the theory that a service-named logger is filtered. Under pytest, which never calls
    ``configure_logging``, an INFO record does need its logger named explicitly; that is a test
    artifact, not the production path.
    """
    logger.info(
        "dispatch batch_id=%s total=%d n_agents=%d subjobs_expected=%d undispatched=%d",
        batch_id,
        total,
        n_agents,
        subjobs_expected,
        undispatched,
    )
    return DispatchOutcome(kind=kind, batch_id=batch_id, total=total, subjobs_expected=subjobs_expected, undispatched=undispatched, status=status)


def _build_chunk_plan(groups: dict[str, list[ExecuteBatchProposalItem]]) -> ChunkPlan:
    """Materialize the whole (agent, chunk) plan once, in ``groups`` order.

    ONE source for ``subjobs_expected``, the dispatch summary's per-agent ``chunks``, and the
    enqueue loop's pending list, so those three can never disagree about the <=500-item chunking.
    """
    return {agent_id: chunk_proposals(items) for agent_id, items in groups.items()}


def _pending_chunks(plan: ChunkPlan) -> PendingChunks:
    """Flatten the plan into the ``(agent_id, chunk_index, chunk)`` list the enqueue loop walks.

    phaze-0t2c: materialized in full BEFORE the first await so a crash part-way through the loop
    can still name the chunks that never landed.
    """
    return [(agent_id, chunk_index, chunk) for agent_id, chunks in plan.items() for chunk_index, chunk in enumerate(chunks)]


def _init_fields(
    *,
    groups: dict[str, list[ExecuteBatchProposalItem]],
    agent_names: dict[str, str],
    plan: ChunkPlan,
    total: int,
    subjobs_expected: int,
) -> dict[str, str]:
    """Build the full ``exec:{batch_id}`` seed mapping, including the per-agent rollup fields.

    ``dispatch_summary`` is the JSON the SSE reader and the reattach path both project their
    per-agent table from, so its key set and ordering are a wire contract, not an internal detail.
    """
    dispatch_summary = [
        {
            "agent_id": agent_id,
            "name": agent_names.get(agent_id, agent_id),
            "chunks": len(plan[agent_id]),
            "total": len(items),
        }
        for agent_id, items in groups.items()
    ]
    fields: dict[str, str] = {
        "total": str(total),
        "completed": "0",
        "failed": "0",
        "copied": "0",
        "verified": "0",
        "deleted": "0",
        "subjobs_completed": "0",
        "subjobs_expected": str(subjobs_expected),
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "dispatch_summary": json.dumps(dispatch_summary),
    }
    for agent_id, items in groups.items():
        fields[f"agent:{agent_id}:total"] = str(len(items))
        fields[f"agent:{agent_id}:completed"] = "0"
        fields[f"agent:{agent_id}:failed"] = "0"
    return fields


async def _seed_batch_hash(redis_client: Redis, key: str, fields: dict[str, str]) -> None:
    """HSET + EXPIRE the batch hash in one transaction (D-04 + RESEARCH Pitfall 4).

    Runs BEFORE the claim (phaze-0t2c): a failure here raises with no sentinel outstanding, and
    the orphaned hash is harmless -- its batch_id was never handed to anyone and it carries the
    same 24h TTL that reaps every other batch.
    """
    async with redis_client.pipeline(transaction=True) as pipe:
        # redis-py types ``mapping``'s key with an invariant union, so a plain ``dict[str, str]``
        # -- which is exactly what this hash holds, every value already stringified -- is not
        # assignable to it. The cast asserts what the dict literally is; it widens nothing.
        pipe.hset(key, mapping=cast("RedisFieldMapping", fields))
        pipe.expire(key, DISPATCH_CLAIM_TTL_SECONDS)
        await pipe.execute()


async def _claim_active_sentinel(
    redis_client: Redis,
    key: str,
    batch_id: uuid.UUID,
    scripts: DispatchScripts,
) -> int:
    """Claim ``execdispatch:active`` for ``batch_id``. Returns 0 (refused), 1 (claimed), 2 (reconciled).

    phaze-fa2p: approved proposals stay APPROVED throughout dispatch -- the SAQ worker only flips
    them to 'executed' much later -- so a second concurrent or repeated POST re-selects the SAME
    rows and enqueues a duplicate move job for every one of them. The sentinel is what stops that.

    phaze-j7u8: the claim goes through the claim-or-reconcile Lua rather than a bare SET NX, so
    the same call that claims also reconciles a sentinel left behind by a dispatch that can no
    longer release it (hash reaped, already terminal, or fully reported but never promoted). That
    is the shared net under all three exec:active wedges.

    phaze-tnp06: this await is inside the settled span. A client TimeoutError after the EVALSHA
    already landed server-side, or Starlette cancelling the handler while suspended here, would
    otherwise leave the sentinel durably claimed with NO way to release it -- no chunk was ever
    enqueued, so no sub-job will ever POST a terminal event, and the just-seeded hash makes the
    NEXT dispatch's claim-reconcile see a live-looking batch and refuse for the full 24h TTL.
    Delete our own hash before re-raising: if the claim landed, the sentinel now names a hash-less
    batch and the next click's reconcile (shape 1, ``EXISTS(held_key) == 0``) takes it over
    outright; if it never landed, deleting a hash nobody was handed is a no-op. Shielded so a
    cancellation already in flight cannot abort the cleanup half-done.
    """
    claim_dispatch = scripts.claim(redis_client)
    try:
        return int(
            await claim_dispatch(
                keys=[ACTIVE_DISPATCH_KEY],
                args=[str(batch_id), str(DISPATCH_CLAIM_TTL_SECONDS), BATCH_KEY_PREFIX],
                client=redis_client,
            ),
        )
    except BaseException:
        await asyncio.shield(redis_client.delete(key))
        raise


async def _reconcile_undispatched(
    redis_client: Redis,
    batch_id: uuid.UUID,
    enqueued_ok: int,
    undispatched_by_agent: dict[str, int],
    scripts: DispatchScripts,
) -> str:
    """Reconcile ``subjobs_expected`` with what ACTUALLY landed, and settle the sentinel. Returns the render status.

    phaze-kxsb: the hash is seeded with the PLANNED ``subjobs_expected`` before the enqueue loop.
    A chunk that never landed will never POST its terminal event, so the promotion could never
    fire and the batch would spin at 'running' until the 24h TTL reaped it. Lower
    ``subjobs_expected`` to the count that landed and count the undispatched proposals as failed so
    the operator sees them, then either promote to terminal directly (nothing landed -> no sub-job
    will ever POST) or re-run the promote check (a landed sub-job may already have reported
    terminal against the stale, higher expected count -- close that race here).

    phaze-0t2c: shared by the success and FAILURE paths so both settle identically. A crash
    mid-enqueue is the same situation as an enqueue exception -- some chunks landed, some never
    will -- and it previously left the batch and the sentinel wedged precisely because only the
    exception path knew how to reconcile.
    """
    key = f"{BATCH_KEY_PREFIX}{batch_id}"
    undispatched_proposals = sum(undispatched_by_agent.values())
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.hset(key, "subjobs_expected", str(enqueued_ok))
        # phaze-1h6j: roll each affected agent's undispatched proposals into that agent's
        # per-agent failed counter (seeded at dispatch, still 0) so completed+failed reaches the
        # agent's total and the row renders a terminal ERRORS/COMPLETE pill instead of freezing
        # on RUNNING. Applied BEFORE the batch-level "failed" hincrby so the batch counter stays
        # the last hincrby for callers that read it positionally.
        for affected_agent_id, agent_undispatched in undispatched_by_agent.items():
            pipe.hincrby(key, f"agent:{affected_agent_id}:failed", agent_undispatched)
        pipe.hincrby(key, "failed", undispatched_proposals)
        if enqueued_ok == 0:
            pipe.hset(key, "status", "complete_with_errors")
        await pipe.execute()

    if enqueued_ok == 0:
        # phaze-fa2p: nothing landed -> this batch is terminal right here and no sub-job will ever
        # POST to release the sentinel via the promote script, so release the claim now.
        # phaze-0t2c: via the CAS release, NOT an unconditional DEL. On the crash path this can run
        # long after the claim was taken, and an unconditional delete would happily drop a LATER
        # dispatch's claim -- re-opening the double-move hazard the sentinel exists to close.
        release = scripts.release(redis_client)
        await release(keys=[ACTIVE_DISPATCH_KEY], args=[str(batch_id)], client=redis_client)
        return "complete_with_errors"

    promote_status = scripts.promote(redis_client)
    # phaze-fa2p: pass the sentinel key + batch_id so, if a landed sub-job already reported
    # terminal, the promotion also releases the single-dispatch claim atomically.
    await promote_status(keys=[key, ACTIVE_DISPATCH_KEY], args=[str(batch_id)], client=redis_client)
    return "running"


async def _enqueue_one_chunk(
    task_router: AgentTaskRouter,
    batch_id: uuid.UUID,
    agent_id: str,
    chunk_index: int,
    chunk: list[ExecuteBatchProposalItem],
) -> bool:
    """Enqueue ONE (agent, chunk) sub-job. Returns True if it counts inside ``subjobs_expected``.

    Log-and-continue on individual failures (PATTERNS S5) so a single bad enqueue does not kill
    the whole dispatch -- a definite failure returns False and is reconciled by the caller.

    phaze-19u7g: an :class:`AmbiguousEnqueueError` returns **True**, not False. It means
    ``enqueue_for_agent`` raised AFTER the broker connection was already live, so the ``saq_jobs``
    INSERT may already be durably committed even though this call never got its ack. Counting it
    as undispatched would roll it into the batch's ``failed`` counters AND lower
    ``subjobs_expected`` past what actually landed, so ``sc >= se`` could promote the batch
    terminal and release the ``execdispatch:active`` sentinel WHILE the phantom sub-job is still
    executing -- an operator retry then double-dispatches the same proposals. Keeping it inside
    ``subjobs_expected`` instead means a genuinely-lost enqueue just leaves the batch running until
    the 24h TTL reaps it, the same non-terminal fate a dispatched-but-never-reported sub-job
    already has (the same trade-off phaze-9f82r made for tag-write enqueues).

    A ``BaseException`` -- a cancellation landing between the broker commit and this coroutine's
    resume -- is NOT caught here: it propagates so the caller's settle can treat that chunk as
    ambiguous in exactly the same sense.
    """
    try:
        await task_router.enqueue_for_agent(
            agent_id=agent_id,
            task_name="execute_approved_batch",
            payload=ExecuteApprovedBatchPayload(
                batch_id=batch_id,
                agent_id=agent_id,
                proposals=chunk,
                sub_batch_index=chunk_index,
            ),
        )
    except AmbiguousEnqueueError:
        logger.error(
            "dispatch: enqueue ambiguous for agent=%s chunk=%s batch_id=%s -- broker "
            "connection was live, chunk may have landed; keeping it inside "
            "subjobs_expected rather than risking a duplicate dispatch",
            agent_id,
            chunk_index,
            batch_id,
            exc_info=True,
        )
    except Exception:
        logger.exception(
            "dispatch: enqueue failed for agent=%s chunk=%s batch_id=%s",
            agent_id,
            chunk_index,
            batch_id,
        )
        return False
    return True


async def _enqueue_all_chunks(
    *,
    redis_client: Redis,
    task_router: AgentTaskRouter,
    batch_id: uuid.UUID,
    pending: PendingChunks,
    scripts: DispatchScripts,
) -> tuple[int, dict[str, int]]:
    """Drive the whole per-(agent, chunk) enqueue plan. Returns ``(enqueued_ok, undispatched_by_agent)``.

    phaze-1h6j: undispatched proposals are tracked PER AGENT, not as a batch-wide scalar, so the
    reconcile can roll them into each affected agent's per-agent failed counter -- otherwise that
    agent's row never reaches completed+failed == total and stays stuck on a RUNNING pill in the
    final render even though the batch is terminal.

    phaze-0t2c: ``pending`` is materialized by the caller BEFORE the first await so a crash
    part-way through can still name the chunks that never landed. Without it the handler below
    could not tell "9 chunks planned, 3 landed" from "3 chunks planned, 3 landed", and would leave
    ``subjobs_expected`` at the planned figure -- a target the survivors can never reach. The loop
    spans many suspension points and real wall time on a large approved set, so a hard failure (or
    a cancellation) can land mid-way with the hash seeded for the PLANNED count and only some
    chunks enqueued; settle it on the way out. ``BaseException`` (not ``Exception``) so a
    ``CancelledError`` settles too, and the settle itself is shielded so a cancellation already in
    flight cannot abort it half-done.
    """
    enqueued_ok = 0
    undispatched_by_agent: dict[str, int] = {}
    attempted = 0
    try:
        for agent_id, chunk_index, chunk in pending:
            if await _enqueue_one_chunk(task_router, batch_id, agent_id, chunk_index, chunk):
                enqueued_ok += 1
            else:
                undispatched_by_agent[agent_id] = undispatched_by_agent.get(agent_id, 0) + len(chunk)
            attempted += 1
    except BaseException:
        # phaze-19u7g: ``attempted`` is only incremented AFTER a chunk's attempt returns, so a
        # BaseException escaping the ``await enqueue_for_agent(...)`` itself -- a CancelledError
        # landing between the broker commit and this coroutine's resume, e.g. a client disconnect
        # cancelling the handler mid-await -- lands here with ``pending[attempted]`` naming THAT
        # in-flight chunk, not one that was never started. Its enqueue may have already committed
        # on the Postgres broker, so it is ambiguous in exactly the same sense as
        # ``AmbiguousEnqueueError``: count it as landed (kept inside ``subjobs_expected``) rather
        # than rolling it into ``undispatched_by_agent``, which would let the batch promote
        # terminal and release the sentinel while that phantom sub-job is still executing. Only
        # the chunks strictly AFTER it were genuinely never attempted at all.
        if attempted < len(pending):
            in_flight_agent_id, in_flight_chunk_index, _in_flight_chunk = pending[attempted]
            logger.error(
                "dispatch: enqueue ambiguous (interrupted mid-await) for agent=%s chunk=%s "
                "batch_id=%s -- keeping it inside subjobs_expected rather than risking a "
                "duplicate dispatch",
                in_flight_agent_id,
                in_flight_chunk_index,
                batch_id,
            )
            enqueued_ok += 1
            attempted += 1
        for agent_id, _chunk_index, chunk in pending[attempted:]:
            undispatched_by_agent[agent_id] = undispatched_by_agent.get(agent_id, 0) + len(chunk)
        await asyncio.shield(_reconcile_undispatched(redis_client, batch_id, enqueued_ok, undispatched_by_agent, scripts))
        raise
    return enqueued_ok, undispatched_by_agent


async def dispatch_approved_batch(
    *,
    redis_client: Redis,
    task_router: AgentTaskRouter,
    groups: dict[str, list[ExecuteBatchProposalItem]],
    agent_names: dict[str, str],
    scripts: DispatchScripts,
) -> DispatchOutcome:
    """Seed, claim, enqueue and reconcile one Execute Approved dispatch. No database, by design.

    ``groups`` is the already-filtered per-agent APPROVED set and ``agent_names`` its display-name
    map; both are produced by the caller's database reads, which are finished and committed before
    this is entered (see the module docstring).

    Step order is the contract -- seed (phaze-0t2c) -> claim (phaze-fa2p / phaze-j7u8) -> enqueue
    -> reconcile (phaze-kxsb). An empty ``groups`` short-circuits before any of it: seeding a
    status="running" hash with a TTL and no sub-jobs would mislead the SSE reader, and there is
    nothing to double-dispatch, so there is nothing to guard.
    """
    batch_id = uuid.uuid4()
    total = sum(len(items) for items in groups.values())
    if not groups:
        return _dispatched(DispatchOutcomeKind.EMPTY, batch_id=batch_id, total=total, subjobs_expected=0, n_agents=0)

    plan = _build_chunk_plan(groups)
    subjobs_expected = sum(len(chunks) for chunks in plan.values())
    key = f"{BATCH_KEY_PREFIX}{batch_id}"

    await _seed_batch_hash(
        redis_client,
        key,
        _init_fields(groups=groups, agent_names=agent_names, plan=plan, total=total, subjobs_expected=subjobs_expected),
    )

    claim_result = await _claim_active_sentinel(redis_client, key, batch_id, scripts)
    if claim_result == 0:
        # Refused. Drop the hash seeded above -- this batch_id is never returned to anyone, so
        # the hash would otherwise sit unread for 24h and, worse, look like a live 'running'
        # batch to anything that enumerates the namespace.
        await redis_client.delete(key)
        # Built directly, NOT through _dispatched: no D-11 line, because nothing was dispatched.
        return DispatchOutcome(kind=DispatchOutcomeKind.REFUSED, batch_id=batch_id, total=total, subjobs_expected=subjobs_expected)
    if claim_result == 2:
        # Not routine. A previous dispatch leaked its claim, so surface it: the reconcile keeps
        # the operator working, but the leak itself is a defect worth seeing in the logs.
        logger.warning(
            "dispatch: reconciled a stale exec:active sentinel before claiming batch_id=%s",
            batch_id,
        )

    enqueued_ok, undispatched_by_agent = await _enqueue_all_chunks(
        redis_client=redis_client,
        task_router=task_router,
        batch_id=batch_id,
        pending=_pending_chunks(plan),
        scripts=scripts,
    )

    undispatched = sum(undispatched_by_agent.values())
    if not undispatched:
        return _dispatched(DispatchOutcomeKind.STARTED, batch_id=batch_id, total=total, subjobs_expected=subjobs_expected, n_agents=len(groups))

    status = await _reconcile_undispatched(redis_client, batch_id, enqueued_ok, undispatched_by_agent, scripts)
    return _dispatched(
        DispatchOutcomeKind.PARTIAL,
        batch_id=batch_id,
        total=total,
        subjobs_expected=enqueued_ok,
        n_agents=len(groups),
        undispatched=undispatched,
        status=status,
    )
