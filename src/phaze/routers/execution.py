"""Execution UI router -- execute button, SSE progress, and audit log.

Phase 28 D-09 + D-11 rewrite: ``start_execution`` now fans out approved
proposals by ``FileRecord.agent_id``, chunks each group at 500, seeds the
``exec:{batch_id}`` Redis hash (D-04), and enqueues one sub-job per
(agent, chunk) via ``AgentTaskRouter.enqueue_for_agent``. ``execution_progress``
emits three SSE event types every tick (``progress``, ``agents_table``,
plus a one-shot ``dispatch_summary`` on first connect), yields a status
event on either ``complete`` or ``complete_with_errors``, and then (phaze-047gd)
a final canonical ``close`` event that the ``sse-close`` listener on the
template's ``sse-connect`` element uses to actually stop the browser's
EventSource -- htmx-ext-sse never reads ``sse-close`` from a descendant, so a
status-named close event alone (the pre-phaze-047gd markup) registers no
listener and the client auto-reconnects forever.

The application server is the sole writer of the ``exec:{batch_id}`` hash via
HSET at dispatch; HINCRBY mutations come exclusively from the Plan 28-02 POST
endpoint (``routers/agent_exec_batches.py``). Both writers use
``app.state.redis`` (decode_responses=True) so the SSE reader gets ``str``,
not ``bytes``.

phaze-a6hm.8: the per-agent rollup table (``agents_table.html``) composes
``phaze.routers.column_sort`` for its header whitelist/resolve/aria-sort
machinery, with ONE necessary adaptation documented on ``EXEC_AGENTS_SORT``
below -- this table has no backing SQL SELECT, so the actual reorder happens
in Python against the (small, whole, never-paginated) per-batch agent list
rather than via ``SortState.order_by()`` + ``paged_stmt``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import math
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from redis.exceptions import ResponseError
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse
import structlog

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.execution import ExecutionLog
from phaze.routers.agent_exec_batches import (
    ACTIVE_DISPATCH_KEY,
    BATCH_KEY_PREFIX,
    DISPATCH_CLAIM_TTL_SECONDS,
    _get_claim_dispatch_script,
    _get_promote_status_script,
    _get_release_dispatch_script,
)
from phaze.routers.column_sort import DESCENDING, SortableColumn, SortContract
from phaze.routers.response_shape import DUAL_SHAPE_RESPONSE_HEADERS, wants_fragment
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.collision import detect_collisions
from phaze.services.execution_dispatch import (
    chunk_proposals,
    count_revoked_skipped_proposals,
    get_approved_proposals_grouped_by_agent,
)
from phaze.services.execution_queries import get_execution_log_detail, get_execution_logs_page, get_execution_stats
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MIN_PAGE_SIZE
from phaze.services.pipeline import count_proposal_pending_files
from phaze.web.static import static_asset_url


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from redis.asyncio import Redis
    from redis.commands.core import AsyncScript
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.routers.column_sort import SortState


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# phaze-315t: fingerprinted, cache-forever static asset URLs, registered as a Jinja global for
# every template this router renders. `execution/audit_log.html` no longer needs it directly
# (phaze-uvmcr.3 made it a content-only fragment, hosted directly by the asset links in shell.html), but the
# global stays -- harmless if unused by the current template set, and cheap insurance against a
# future template rendered through this same `templates` instance needing it.
templates.env.globals["static_url"] = static_asset_url
router = APIRouter(tags=["execution"])

# phaze-5zyv: how many consecutive empty (no exec:{batch_id} hash) SSE poll ticks to tolerate
# before the ``execution_progress`` generator gives up and emits a terminal close event. Bounds the
# otherwise-unbounded "Waiting for execution to start..." loop for empty dispatches, reaped/expired
# batches, and unknown batch ids. One tick ~= 1s, so this is roughly the grace window for a hash to
# appear before the stream self-terminates.
_MAX_EMPTY_POLLS = 5

# phaze-a6hm.5: ONE contract, declared at import time next to the handler it serves
# (column_sort.py contract rule 6). ``target`` is "#audit-content" -- the SAME host div the filter
# tabs and pager already swap into (execution/audit_log.html), so a sort click introduces no new
# swap target and no OOB fragment. "Error" stays a plain header: it is a sparsely-populated free-text
# column (most rows carry no error), so ordering by it is not a meaningful operator question the way
# ordering by status, operation, or timestamp is.
AUDIT_SORT = SortContract(
    endpoint="/audit/",
    target="#audit-content",
    columns=(
        SortableColumn(key="operation", label="Operation", expression=ExecutionLog.operation),
        SortableColumn(key="source_path", label="Source Path", expression=ExecutionLog.source_path),
        SortableColumn(key="destination_path", label="Destination Path", expression=ExecutionLog.destination_path),
        SortableColumn(key="sha256_verified", label="SHA256 Verified", expression=ExecutionLog.sha256_verified),
        SortableColumn(key="status", label="Status", expression=ExecutionLog.status),
        SortableColumn(key="executed_at", label="Timestamp", expression=ExecutionLog.executed_at),
    ),
    default_key="executed_at",
    default_order=DESCENDING,
)

# phaze-a6hm.8: the per-agent rollup table's sort whitelist, declared at import time (column_sort
# rule 6) next to the handlers that serve it. UNLIKE every other table wired to this contract so
# far, ``agents_table.html`` has no backing SQL SELECT -- its rows are a Redis hash projection
# (``_agents_view_from_hash`` / ``_build_agents_view``), rebuilt whole on every SSE tick. So each
# ``expression`` here is an ``itemgetter`` over that row dict, not a SQLAlchemy column: the whitelist
# -> concrete-accessor structural guarantee (rule 2's crux -- equality lookup only, never getattr,
# never a name later turned into a column) still holds, it just resolves to a dict accessor instead
# of a column object. ``SortState.order_by()`` assumes SQL and is deliberately UNUSED for this table
# -- reordering happens via ``_sort_agents_view`` below, sorting the Python list directly. That is
# NOT the rule-1 defect (sorting rows already fetched, reordering a PAGE and presenting it as the
# whole corpus): this table is never paginated, so every render already holds the WHOLE per-batch
# agent list, and sorting it in Python sorts the true full set, not a slice of it.
#
# ``endpoint`` carries no ``batch_id`` (unlike a normal path-scoped table endpoint) because a
# SortContract is one frozen object built once at import time (rule 6) and this table's identity
# varies per execution batch. ``batch_id`` instead rides ``view_state`` like any other view
# parameter a header click must preserve (rule 4) -- which is exactly the role it plays here: the
# operator's "which batch am I sorting" is itself part of the view state, no different in kind from
# a stage lens or a page size elsewhere in this contract's other tables.
EXEC_AGENTS_SORT = SortContract(
    endpoint="/execution/agents-table",
    target="#execution-agents-table",
    columns=(
        SortableColumn(key="name", label="Agent", expression=itemgetter("name")),
        SortableColumn(key="completed", label="Completed", expression=itemgetter("completed")),
        SortableColumn(key="failed", label="Failed", expression=itemgetter("failed")),
        SortableColumn(key="total", label="Total", expression=itemgetter("total")),
    ),
    default_key="name",
)
# "Status" is deliberately NOT a sortable column: it is a derived pill (PENDING/RUNNING/COMPLETE/
# ERRORS computed from completed+failed+total in the template), not a raw stored value -- the same
# reason the pipeline.py contracts never offer their derived stage-pill columns for sorting either.


# phaze-pyv3: persist the operator's sort choice onto the batch hash ONLY if the key still exists,
# atomically. ``execution_agents_table_sort`` reads the hash (hgetall) and then writes the sort
# fields; the batch's 24h TTL can fire in the gap between those two round trips, and a plain HSET
# would then RECREATE ``exec:{batch_id}`` with just ``agents_sort``/``agents_order`` and, per Redis
# semantics, NO expiry -- leaking the key forever (the TTL is the only reaper) and feeding any
# attached SSE stream a status-less 2-field hash it renders as a phantom "running 0/0" batch that
# never terminates. The EXISTS check + HSET run in one Redis round trip so the TTL cannot interleave:
# a reaped key is left reaped (returns 0), never resurrected. Returns 1 if persisted, 0 if the key
# was already gone.
_PERSIST_SORT_IF_EXISTS_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'agents_sort', ARGV[1], 'agents_order', ARGV[2])
return 1
"""

# Registered lazily on first use (no Redis handle at import time), cached so subsequent sort clicks
# reuse the EVALSHA fast-path. Mirrors ``agent_exec_batches._get_promote_status_script``.
_persist_sort_script: AsyncScript | None = None


def _get_persist_sort_script(redis_client: Redis) -> AsyncScript:
    """Return the cached EXISTS-guarded sort-persist script, registering it on first call."""
    global _persist_sort_script
    if _persist_sort_script is None:
        _persist_sort_script = redis_client.register_script(_PERSIST_SORT_IF_EXISTS_LUA)
    return _persist_sort_script


def _sort_agents_view(agents_view: list[dict[str, object]], sort_state: SortState) -> list[dict[str, object]]:
    """Reorder the FULL per-agent rollup by the resolved, whitelisted sort (phaze-a6hm.8).

    ``sort_state.key`` is guaranteed to name one of ``EXEC_AGENTS_SORT``'s columns (column_sort
    rule 2/3 -- :meth:`SortContract.resolve` never hands back anything else), so the lookup below can
    only ever reach an ``itemgetter`` some developer wrote down on purpose, never a name derived from
    the request. A stable sort is required so agents that tie on the chosen key keep their
    ``dispatch_summary`` order across ticks instead of visibly shuffling once a second.
    """
    column = next(column for column in sort_state.contract.columns if column.key == sort_state.key)
    return sorted(agents_view, key=column.expression, reverse=sort_state.order == DESCENDING)


def _build_agents_view(
    groups: dict[str, list[ExecuteBatchProposalItem]],
    *,
    agent_names: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Build the per-agent rollup row list consumed by agents_table.html.

    At dispatch time (first render), every agent is at completed=0/failed=0.
    The SSE generator re-renders this view each tick from the Redis hash state.
    """
    agent_names = agent_names or {}
    return [
        {
            "agent_id": agent_id,
            "name": agent_names.get(agent_id, agent_id),
            "completed": 0,
            "failed": 0,
            "total": len(items),
        }
        for agent_id, items in groups.items()
    ]


async def _reconcile_undispatched(
    redis_client: Redis,
    batch_id: uuid.UUID,
    enqueued_ok: int,
    undispatched_by_agent: dict[str, int],
) -> str:
    """Reconcile ``subjobs_expected`` with what ACTUALLY landed, and settle the sentinel. Returns the render status.

    phaze-kxsb: the hash is seeded with the PLANNED ``subjobs_expected`` before the enqueue loop.
    A chunk that never landed will never POST its terminal event, so the promotion could never
    fire and the batch would spin at 'running' until the 24h TTL reaped it. Lower
    ``subjobs_expected`` to the count that landed and count the undispatched proposals as failed so
    the operator sees them, then either promote to terminal directly (nothing landed -> no sub-job
    will ever POST) or re-run the promote check (a landed sub-job may already have reported
    terminal against the stale, higher expected count -- close that race here).

    phaze-0t2c: extracted from ``start_execution`` so the FAILURE path can run the identical
    settle. A crash mid-enqueue is the same situation as an enqueue exception -- some chunks
    landed, some never will -- and it previously left the batch and the sentinel wedged precisely
    because only the exception path knew how to reconcile.
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
        release = _get_release_dispatch_script(redis_client)
        await release(keys=[ACTIVE_DISPATCH_KEY], args=[str(batch_id)], client=redis_client)
        return "complete_with_errors"

    promote_status = _get_promote_status_script(redis_client)
    # phaze-fa2p: pass the sentinel key + batch_id so, if a landed sub-job already reported
    # terminal, the promotion also releases the single-dispatch claim atomically.
    await promote_status(keys=[key, ACTIVE_DISPATCH_KEY], args=[str(batch_id)], client=redis_client)
    return "running"


@router.post("/execution/start", response_class=HTMLResponse)
async def start_execution(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Dispatch approved proposals as per-agent SAQ sub-jobs (Phase 28 D-09).

    Sequence:
      1. Pre-check collisions -- fires before any grouping, but (phaze-p46n4) the
         collision key is already agent-scoped (``FileRecord.agent_id`` is the
         first component of ``_dest_key_columns``) and ``detect_collisions``
         excludes revoked-agent proposals, matching step 2's population exactly.
      2. SELECT + GROUP BY ``FileRecord.agent_id``, filter revoked agents
         (services/execution_dispatch.py).
      3. Generate parent ``batch_id``; compute ``subjobs_expected`` from
         ``ceil(N/500)`` per agent.
      4. Atomic ``HSET`` + ``EXPIRE`` on ``exec:{batch_id}`` via
         ``redis.pipeline(transaction=True)`` (D-04 + RESEARCH Pitfall 4).
      5. Per-(agent, chunk) enqueue loop, best-effort log-and-continue on
         failures (PATTERNS S5).
      6. INFO log line per D-11.
      7. Return the progress card with first-render context.
    """
    # 1. Pre-check collision -- collision_block short-circuits dispatch. Revoked-agent proposals
    # are excluded here too (phaze-p46n4), so this gate's population matches step 2's exactly.
    collisions = await detect_collisions(session)
    if collisions:
        return templates.TemplateResponse(
            request=request,
            name="execution/partials/collision_block.html",
            context={"request": request, "collisions": collisions},
        )

    # 2. Group + filter revoked.
    groups = await get_approved_proposals_grouped_by_agent(session)
    skipped_revoked = await count_revoked_skipped_proposals(session)

    # 3. Parent batch_id + totals.
    batch_id = uuid.uuid4()
    total = sum(len(items) for items in groups.values())
    subjobs_expected = sum(math.ceil(len(items) / 500) for items in groups.values())

    # 4. Resolve per-agent display names (so the table + dispatch_summary
    # render the human-readable name, not just the slug). We re-query Agent
    # rows because the grouping service returns wire-format items only.
    agent_names: dict[str, str] = {}
    if groups:
        result = await session.execute(select(Agent.id, Agent.name).where(Agent.id.in_(groups.keys())))
        agent_names = {row.id: row.name for row in result.all()}

    # 5. Seed exec:{batch_id} Redis hash (D-04). HSET + EXPIRE atomic via pipeline.
    dispatch_summary = [
        {
            "agent_id": agent_id,
            "name": agent_names.get(agent_id, agent_id),
            "chunks": math.ceil(len(items) / 500),
            "total": len(items),
        }
        for agent_id, items in groups.items()
    ]

    init_fields: dict[str, str] = {
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
        init_fields[f"agent:{agent_id}:total"] = str(len(items))
        init_fields[f"agent:{agent_id}:completed"] = "0"
        init_fields[f"agent:{agent_id}:failed"] = "0"

    redis_client = request.app.state.redis
    key = f"exec:{batch_id}"

    # 5b. phaze-0t2c: SEED FIRST, THEN CLAIM. The original order took the claim at the top and
    # seeded immediately after, and those two commands are not one transaction: a transient Redis
    # fault on the seed left the sentinel claimed against a batch whose hash never existed, so
    # nothing could ever release it and every Execute Approved for the next 24h was refused. The
    # asymmetry was that the claim was taken BEFORE the only two things that can make it releasable
    # (the hash, and at least one landed sub-job). Seeding first removes that window entirely: a
    # failed seed raises with no claim outstanding, and the orphaned hash is harmless -- its
    # batch_id was never handed to anyone and it carries the same 24h TTL.
    if groups:
        # Only seed when there is at least one (agent, chunk) to dispatch. An
        # empty hash with status="running" and TTL would mislead the SSE reader.
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=init_fields)
            pipe.expire(key, DISPATCH_CLAIM_TTL_SECONDS)
            await pipe.execute()

    # phaze-fa2p: single-dispatch guard. Approved proposals stay APPROVED throughout dispatch --
    # they are only flipped to 'executed' asynchronously by the SAQ worker much later -- so a
    # second concurrent or repeated POST re-selects the SAME still-APPROVED rows and enqueues a
    # duplicate move job for every one of them (each dispatch mints its own batch_id, so SAQ never
    # dedups). Atomically claim the ``exec:active`` sentinel before enqueuing; a competing dispatch
    # loses the claim and is refused until the active batch reaches a terminal status (the promote
    # script releases the sentinel) or the 24h safety TTL elapses. Only guard when there is actually
    # something to dispatch -- an empty dispatch enqueues nothing.
    #
    # phaze-j7u8: the claim goes through ``_CLAIM_DISPATCH_LUA`` rather than a bare SET NX, so the
    # same call that claims also RECONCILES a sentinel left behind by a dispatch that can no longer
    # release it (batch hash reaped, already terminal, or fully reported but never promoted). That
    # is the shared net under all three exec:active wedges -- without it, one lost terminal event
    # or one crash in the claim window costs the operator the whole execute stage for 24h.
    if groups:
        claim_dispatch = _get_claim_dispatch_script(redis_client)
        try:
            claim_result = int(
                await claim_dispatch(
                    keys=[ACTIVE_DISPATCH_KEY],
                    args=[str(batch_id), str(DISPATCH_CLAIM_TTL_SECONDS), BATCH_KEY_PREFIX],
                    client=redis_client,
                ),
            )
        except BaseException:
            # phaze-tnp06: unlike the enqueue loop below, this await was NOT inside the
            # BaseException-settled span (phaze-0t2c) -- a redis client TimeoutError after the
            # EVALSHA already landed server-side, or Starlette cancelling the handler on client
            # disconnect while suspended here, left ``execdispatch:active`` durably claimed with
            # NO way to release it: no chunk was ever enqueued, so no sub-job will ever POST a
            # terminal event to promote/release it, and the hash this claim would reconcile
            # against (``key``, seeded just above per phaze-0t2c's seed-first order) makes the
            # NEXT dispatch's claim-reconcile see a live-looking hash and refuse for the full 24h
            # TTL -- the exact wedge phaze-j7u8 was built to close, reopened through this one
            # await. Delete our own just-seeded hash before re-raising: if the claim actually
            # landed server-side, the sentinel now names a hash-less batch and the next click's
            # claim-reconcile (shape 1, ``EXISTS(held_key) == 0``) takes it over outright; if it
            # never landed, deleting a hash nobody was ever handed is a no-op. Shielded so a
            # cancellation already in flight cannot abort the cleanup half-done.
            await asyncio.shield(redis_client.delete(key))
            raise
        if claim_result == 0:
            # Refused. Drop the hash seeded above -- this batch_id is never returned to anyone, so
            # the hash would otherwise sit unread for 24h and, worse, look like a live 'running'
            # batch to anything that enumerates the namespace.
            await redis_client.delete(key)
            # phaze-2tsw9: re-attach to the batch that's ACTUALLY running instead of a dead-end
            # alert -- both land in the same #apply-execute-response sink (hx-swap="innerHTML"), so
            # the refusal would otherwise evict the live progress card and its only sse-connect
            # mount, with the running batch_id unrecoverable from the UI afterwards.
            reattached = await _reattach_active_progress(request, redis_client)
            if reattached is not None:
                return reattached
            return templates.TemplateResponse(
                request=request,
                name="execution/partials/dispatch_in_progress.html",
                context={"request": request},
            )
        if claim_result == 2:
            # Not routine. A previous dispatch leaked its claim, so surface it: the reconcile keeps
            # the operator working, but the leak itself is a defect worth seeing in the logs.
            logger.warning(
                "dispatch: reconciled a stale exec:active sentinel before claiming batch_id=%s",
                batch_id,
            )

    # 6. Per-(agent, chunk) enqueue. Log-and-continue on individual failures
    # (PATTERNS S5) so a single bad enqueue does not kill the whole dispatch.
    task_router = request.app.state.task_router
    enqueued_ok = 0
    # phaze-1h6j: track undispatched proposals PER AGENT (not just a batch-wide scalar) so the
    # reconcile below can roll them into each affected agent's per-agent failed counter -- otherwise
    # that agent's row never reaches completed+failed == total and stays stuck on a RUNNING pill in
    # the final render even though the batch is terminal.
    undispatched_by_agent: dict[str, int] = {}
    # phaze-0t2c: materialize the whole (agent, chunk) plan up front so a crash PART-WAY through the
    # loop can still name the chunks that never landed. Without it the finally below could not tell
    # "9 chunks planned, 3 landed" from "3 chunks planned, 3 landed", and would leave
    # subjobs_expected at the planned figure -- a target the surviving sub-jobs can never reach.
    pending = [(agent_id, chunk_index, chunk) for agent_id, items in groups.items() for chunk_index, chunk in enumerate(chunk_proposals(items))]
    attempted = 0
    try:
        for agent_id, chunk_index, chunk in pending:
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
                enqueued_ok += 1
            except AmbiguousEnqueueError:
                # phaze-19u7g: ``enqueue_for_agent`` raised AFTER the broker connection was
                # already live (see ``AmbiguousEnqueueError``'s docstring) -- the ``saq_jobs``
                # INSERT may already be durably committed even though this call never got its
                # ack. Counting it into ``undispatched_by_agent`` (as a plain ``Exception`` does,
                # below) would roll it into the batch's ``failed`` counters AND lower
                # ``subjobs_expected`` past what actually landed, so ``sc >= se`` could promote
                # the batch terminal and release the ``execdispatch:active`` sentinel WHILE the
                # phantom sub-job is still executing -- an operator retry then double-dispatches
                # the same proposals (phaze-19u7g's failure scenario). Count it as landed instead:
                # a genuinely-lost enqueue then just leaves the batch running until the 24h TTL
                # reaps it, the same non-terminal fate a dispatched-but-never-reported sub-job
                # already has -- the same trade-off phaze-9f82r made for tag-write enqueues.
                logger.error(
                    "dispatch: enqueue ambiguous for agent=%s chunk=%s batch_id=%s -- broker "
                    "connection was live, chunk may have landed; keeping it inside "
                    "subjobs_expected rather than risking a duplicate dispatch",
                    agent_id,
                    chunk_index,
                    batch_id,
                    exc_info=True,
                )
                enqueued_ok += 1
            except Exception:
                logger.exception(
                    "dispatch: enqueue failed for agent=%s chunk=%s batch_id=%s",
                    agent_id,
                    chunk_index,
                    batch_id,
                )
                undispatched_by_agent[agent_id] = undispatched_by_agent.get(agent_id, 0) + len(chunk)
            attempted += 1
    except BaseException:
        # phaze-0t2c: the loop spans many suspension points and real wall time on a large approved
        # set, so a hard failure (or a cancellation) can land mid-way with the hash seeded for the
        # PLANNED subjobs_expected and only some chunks enqueued. subjobs_completed could then never
        # reach subjobs_expected, the promote would never fire, and the sentinel would be held for
        # the full 24h. Settle it on the way out: the chunks we never reached are counted as
        # undispatched, exactly as an enqueue exception would have counted them, and the same
        # reconcile runs. BaseException (not Exception) so a CancelledError settles too; the settle
        # itself is shielded so a cancellation already in flight cannot abort it half-done.
        #
        # phaze-19u7g: ``attempted`` is only incremented AFTER a chunk's inner try/except finishes
        # (line ~453 above), so a BaseException escaping the ``await enqueue_for_agent(...)`` itself
        # -- a CancelledError landing between the broker commit and this coroutine's resume, e.g. a
        # client disconnect cancelling the handler mid-await -- lands here with ``pending[attempted]``
        # naming THAT in-flight chunk, not one that was never started. Its enqueue may have already
        # committed on the Postgres broker, so it is ambiguous in exactly the same sense as
        # ``AmbiguousEnqueueError`` above: count it as landed (kept inside ``subjobs_expected``)
        # rather than rolling it into ``undispatched_by_agent``, which would let the batch promote
        # terminal and release the sentinel while that phantom sub-job is still executing. Only the
        # chunks strictly AFTER it were genuinely never attempted at all.
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
        if groups:
            await asyncio.shield(_reconcile_undispatched(redis_client, batch_id, enqueued_ok, undispatched_by_agent))
        raise
    undispatched_proposals = sum(undispatched_by_agent.values())

    render_status = "running"
    if groups and undispatched_proposals:
        render_status = await _reconcile_undispatched(redis_client, batch_id, enqueued_ok, undispatched_by_agent)
        subjobs_expected = enqueued_ok

    # 7. D-11 dispatch INFO log.
    logger.info(
        "dispatch batch_id=%s total=%d n_agents=%d subjobs_expected=%d undispatched=%d",
        batch_id,
        total,
        len(groups),
        subjobs_expected,
        undispatched_proposals,
    )

    # 8. First-render context for progress.html. phaze-a6hm.8: a fresh dispatch has no operator
    # sort choice yet, so resolve() with sort=None/order=None -- degrades to the contract's default
    # (rule 3), exactly like every other table's unvisited state.
    sort_state = EXEC_AGENTS_SORT.resolve(view_state={"batch_id": str(batch_id)})
    return templates.TemplateResponse(
        request=request,
        name="execution/partials/progress.html",
        context={
            "request": request,
            "batch_id": str(batch_id),
            "skipped_revoked": skipped_revoked,
            "total": total,
            "completed": 0,
            # phaze-kxsb: undispatched proposals (chunks that failed to enqueue) are surfaced as
            # failed at first render, and the status is already terminal when NOTHING landed.
            "failed": undispatched_proposals,
            "subjobs_expected": subjobs_expected,
            "agents": _sort_agents_view(_build_agents_view(groups, agent_names=agent_names), sort_state),
            "sort": sort_state,
            "status": render_status,
        },
    )


def _coerce_int(value: object, default: int = 0) -> int:
    """Best-effort int() coercion for object-typed values (dispatch_summary / Redis hash)."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _agents_view_from_hash(
    data: dict[str, str],
    dispatch_summary: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Project the Redis hash + dispatch_summary into per-agent rollup rows.

    Iterates the agents in dispatch_summary order so re-renders stay stable.
    """
    rows: list[dict[str, object]] = []
    for item in dispatch_summary:
        agent_id = str(item.get("agent_id", ""))
        fallback_total = _coerce_int(item.get("total"), 0)
        rows.append(
            {
                "agent_id": agent_id,
                "name": item.get("name", agent_id),
                "completed": _coerce_int(data.get(f"agent:{agent_id}:completed"), 0),
                "failed": _coerce_int(data.get(f"agent:{agent_id}:failed"), 0),
                "total": _coerce_int(data.get(f"agent:{agent_id}:total"), fallback_total),
            }
        )
    return rows


async def _hgetall_or_empty(redis_client: Redis, key: str) -> dict[str, str]:
    """HGETALL ``key``, degrading a wrong-key-type reply to the empty state (phaze-c3j0).

    Belt to the ``uuid.UUID`` parameter typing's braces. With the namespace split and the typed
    parameter there is no longer a spelling of ``batch_id`` that can name a non-hash key, but this
    is a read on the operator's live progress view: if some future key ever collides again, the
    right failure is the empty-state render / terminal SSE close both call sites already handle for
    a reaped batch -- not a 500 for one caller and a silently dead stream for the other.
    """
    try:
        # The shared client is wired with decode_responses=True (main.lifespan, Phase 26 D-27), so
        # this really is str->str; redis-py's own type is the undecoded union.
        data: dict[str, str] = await redis_client.hgetall(key)  # type: ignore[assignment]
    except ResponseError:
        logger.warning("execution progress: %s is not a hash -- rendering the empty state", key)
        return {}
    return data


async def _reattach_active_progress(request: Request, redis_client: Redis) -> HTMLResponse | None:
    """Re-render ``progress.html`` for the CURRENTLY RUNNING batch, or ``None`` if there isn't one.

    phaze-2tsw9: a refused dispatch (the ``exec:active`` sentinel already held) used to render the
    static ``dispatch_in_progress.html`` alert straight into ``#apply-execute-response`` -- the same
    ``hx-swap="innerHTML"`` sink the LIVE progress card already occupies (the button's only trigger,
    ``pipeline/partials/apply_workspace.html``, targets that one node for all three response shapes).
    That swap evicted the running batch's progress card and, with it, its only ``sse-connect``
    mount -- the batch's ``batch_id`` is not surfaced anywhere else in the UI, so there was no way to
    reattach and the operator's only remaining visibility was the audit log.

    ``ACTIVE_DISPATCH_KEY``'s VALUE *is* the running batch_id (the promote script only releases the
    sentinel ``if GET(active_key) == batch_id``), so it can be read back here and used to rebuild the
    SAME context ``start_execution``'s first render and the SSE ``agents_table`` re-sort endpoint
    already build from the ``exec:{batch_id}`` hash. Returns ``None`` (never raises) when there is no
    active batch, or the batch's hash has already reaped out from under a very tight race -- either
    way the caller falls back to the static refusal card, which is still correct in that case.
    """
    # The shared client is wired with decode_responses=True (main.lifespan, Phase 26 D-27), so this
    # really is str | None; redis-py's own stub types GET as the undecoded union.
    active_batch_id: str | None = await redis_client.get(ACTIVE_DISPATCH_KEY)  # type: ignore[assignment]
    if not active_batch_id:
        return None
    batch_key = f"{BATCH_KEY_PREFIX}{active_batch_id}"
    data = await _hgetall_or_empty(redis_client, batch_key)
    if not data:
        return None

    total = int(data.get("total", 0))
    completed = int(data.get("completed", 0))
    failed = int(data.get("failed", 0))
    status = data.get("status", "running")
    try:
        dispatch_summary: list[dict[str, object]] = json.loads(data.get("dispatch_summary", "[]"))
    except json.JSONDecodeError:
        dispatch_summary = []

    agents_view = _agents_view_from_hash(data, dispatch_summary)
    sort_state = EXEC_AGENTS_SORT.resolve(
        sort=data.get("agents_sort"),
        order=data.get("agents_order"),
        view_state={"batch_id": str(active_batch_id)},
    )

    return templates.TemplateResponse(
        request=request,
        name="execution/partials/progress.html",
        context={
            "request": request,
            "batch_id": str(active_batch_id),
            # skipped_revoked is a first-render-only figure (never persisted to the hash) --
            # omitted here, same as every SSE re-render already does.
            "total": total,
            "completed": completed,
            "failed": failed,
            "subjobs_expected": int(data.get("subjobs_expected", 0)),
            "agents": _sort_agents_view(agents_view, sort_state),
            "sort": sort_state,
            "status": status,
        },
    )


def _render_partial(request: Request, name: str, context: dict[str, object]) -> str:
    """Render a Jinja partial through FastAPI's ``Jinja2Templates`` wrapper.

    Returns the decoded HTML body. Routes the rendering through
    ``templates.TemplateResponse`` so autoescape + the project's standard
    template chain stay consistent with the rest of the app -- avoids reaching
    into ``templates.env`` directly.
    """
    response = templates.TemplateResponse(request=request, name=name, context={"request": request, **context})
    body = response.body
    if isinstance(body, memoryview):
        body = bytes(body)
    return body.decode()


@router.get("/execution/progress/{batch_id}")
async def execution_progress(request: Request, batch_id: uuid.UUID) -> EventSourceResponse:
    """Stream SSE events with real-time execution progress from Redis (D-04 + D-11).

    Event sequence per poll tick (1s cadence):
      - ``dispatch_summary`` (ONCE, on first connect with non-empty hash) --
        rendered HTML of the heading line.
      - ``progress`` (every tick) -- rendered HTML of the aggregate counter row.
      - ``agents_table`` (every tick) -- rendered HTML of the per-agent table.

    On terminal status (``complete`` or ``complete_with_errors``) the generator
    yields the final ``progress`` + ``agents_table`` events for that state,
    the matching status event, then a canonical ``close`` event (phaze-047gd)
    before returning -- the ``close`` event is what the template's
    ``sse-close="close"`` listener actually reacts to; the status-named event
    alone does not close the browser's EventSource (htmx-ext-sse only reads
    ``sse-close`` from the ``sse-connect`` element, and only one listener can
    live there).

    phaze-5zyv: the ``not data`` (empty-hash) branch is bounded by
    ``_MAX_EMPTY_POLLS``. A batch that never seeds a hash (empty dispatch), one
    whose 24h TTL has elapsed, or a mistyped/stale ``batch_id`` would otherwise
    loop "Waiting for execution to start..." forever, holding the connection and
    polling Redis every second until the client disconnects. After the cap the
    generator emits a terminal ``complete`` close event and returns so the stream
    always terminates on its own.

    phaze-c3j0: ``batch_id`` is typed ``uuid.UUID``, matching the ``uuid.uuid4()`` the dispatcher
    actually mints, so FastAPI 422s a malformed id before any Redis call. That is what closes the
    key-aliasing defect: the parameter used to be a free-form ``str`` interpolated straight into
    ``exec:{batch_id}``, so the literal value ``active`` reconstructed the ``exec:active`` STRING
    sentinel and HGETALL against it raised WRONGTYPE from inside this generator -- before the first
    yield, so the stream emitted nothing at all, never the terminal close event it documents.
    """
    redis_client = request.app.state.redis
    batch_key = f"{BATCH_KEY_PREFIX}{batch_id}"

    async def event_generator() -> AsyncGenerator[dict[str, str]]:
        first_connect = True
        empty_polls = 0
        while True:
            data = await _hgetall_or_empty(redis_client, batch_key)
            if not data:
                empty_polls += 1
                if empty_polls >= _MAX_EMPTY_POLLS:
                    # No hash ever appeared (empty dispatch, reaped/expired batch, or an unknown
                    # batch_id). Close the stream with a terminal event rather than polling forever.
                    yield {
                        "event": "complete",
                        "data": 'This execution is no longer available. <a href="/s/audit" class="text-blue-600 hover:underline ml-2">View Audit Log</a>',
                    }
                    # phaze-047gd: the browser's EventSource only stops reconnecting on an
                    # sse-close-registered event; htmx-ext-sse reads sse-close exclusively from the
                    # element carrying sse-connect (progress.html), never from a descendant, so both
                    # terminal paths emit this same canonical close event for that listener to catch.
                    yield {"event": "close", "data": ""}
                    return
                yield {"event": "progress", "data": "Waiting for execution to start..."}
                await asyncio.sleep(1)
                continue

            total = int(data.get("total", 0))
            completed = int(data.get("completed", 0))
            failed = int(data.get("failed", 0))
            status = data.get("status", "running")
            try:
                dispatch_summary: list[dict[str, object]] = json.loads(data.get("dispatch_summary", "[]"))
            except json.JSONDecodeError:
                dispatch_summary = []

            agents_view = _agents_view_from_hash(data, dispatch_summary)

            # phaze-a6hm.8: re-resolve the operator's sort choice every tick from the SAME hash the
            # rest of this loop already reads. ``/execution/agents-table`` (below) is the only writer
            # of ``agents_sort``/``agents_order``, and it writes ONLY whitelisted values -- but
            # resolve() re-validates them anyway (never trust a stored value either), so a hand-edited
            # or stale hash degrades to the default order rather than erroring the whole poll.
            sort_state = EXEC_AGENTS_SORT.resolve(
                sort=data.get("agents_sort"),
                order=data.get("agents_order"),
                view_state={"batch_id": str(batch_id)},
            )

            # First-connect dispatch_summary event (D-11 / UI-SPEC C1 step 2).
            if first_connect:
                first_connect = False
                summary_html = _render_partial(
                    request,
                    "execution/partials/dispatch_summary_inline.html",
                    {
                        "total": total,
                        "agents": agents_view,
                        "subjobs_expected": int(data.get("subjobs_expected", 0)),
                    },
                )
                yield {"event": "dispatch_summary", "data": summary_html}

            # Every-tick aggregate progress event (preserves Phase 25 event name).
            progress_html = _render_partial(
                request,
                "execution/partials/progress_row_inline.html",
                {"total": total, "completed": completed, "failed": failed},
            )
            yield {"event": "progress", "data": progress_html}

            # Every-tick agents_table event (UI-SPEC C2). phaze-a6hm.8: sorted by the resolved state
            # so the header's active caret/aria-sort stays correct across every re-render, not just
            # the one that followed the click.
            agents_html = _render_partial(
                request,
                "execution/partials/agents_table.html",
                {"agents": _sort_agents_view(agents_view, sort_state), "sort": sort_state, "batch_id": str(batch_id)},
            )
            yield {"event": "agents_table", "data": agents_html}

            # Terminal status: close on either complete OR complete_with_errors
            # (CONTEXT specifics line 264 widens the existing single-status check).
            if status in {"complete", "complete_with_errors"}:
                if failed == 0:
                    msg = f'Execution complete. All {total} files renamed successfully. <a href="/s/audit" class="text-blue-600 hover:underline ml-2">View Audit Log</a>'
                else:
                    msg = f'Execution complete. {completed} succeeded, {failed} failed. <a href="/s/audit" class="text-blue-600 hover:underline ml-2">View Audit Log</a>'
                yield {"event": status, "data": msg}
                # phaze-047gd: see the empty-hash terminal path above for why this second yield is
                # required -- the sse-close listener lives on the sse-connect element and only ever
                # fires on this canonical "close" event name.
                yield {"event": "close", "data": ""}
                return

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@router.get("/execution/agents-table", response_class=HTMLResponse)
async def execution_agents_table_sort(
    request: Request,
    batch_id: uuid.UUID = Query(...),
    sort: str | None = Query(None),
    order: str | None = Query(None),
) -> HTMLResponse:
    """Re-render the per-agent table under a new header-chosen sort (phaze-a6hm.8, EXEC_AGENTS_SORT).

    This table is SSE-pushed (``execution_progress`` re-renders it every tick from the same
    ``exec:{batch_id}`` hash), so a header click cannot simply swap a re-sorted fragment into place
    the way a plain GET-rendered table does -- the next poll tick would silently overwrite it with
    the default order within ~1s (dropping the "sorting preserves view state" guarantee, contract
    rule 4, on its own timer). So this handler does two things, both gated through the SAME
    ``resolve()`` call:

      1. Persists the RESOLVED (whitelisted, never the raw wire value) sort onto the batch's hash, so
         every subsequent SSE tick's ``EXEC_AGENTS_SORT.resolve()`` picks it up and keeps honouring it.
      2. Returns the freshly-sorted fragment immediately, so the click has the SAME instant feedback
         as every other sortable table instead of waiting on the next tick.

    A batch that has already reaped (empty hash -- 24h TTL, or a stale/garbage-collected id) renders
    the same empty state ``agents_table.html`` already shows for zero agents, matching the SSE
    reader's own empty-hash handling; it does not 404 a poll for a batch that simply finished.

    phaze-c3j0: ``batch_id`` is typed ``uuid.UUID`` for the same reason as the SSE route above --
    a free-form ``str`` let ``?batch_id=active`` rebuild the ``exec:active`` STRING sentinel, and
    the resulting WRONGTYPE escaped as an unhandled 500, contradicting the empty-state promise this
    docstring makes. The write below is already gated behind ``if data:``, so the read failing shut
    is enough; there was never a corruption path, only a wrong status code.
    """
    redis_client = request.app.state.redis
    key = f"{BATCH_KEY_PREFIX}{batch_id}"
    data = await _hgetall_or_empty(redis_client, key)
    sort_state = EXEC_AGENTS_SORT.resolve(sort=sort, order=order, view_state={"batch_id": str(batch_id)})

    agents_view: list[dict[str, object]] = []
    if data:
        # phaze-pyv3: persist the sort atomically behind an EXISTS guard. The hgetall above and this
        # write are separate round trips; if the batch's 24h TTL fired between them a plain HSET
        # would resurrect exec:{batch_id} as a TTL-less 2-field key (leaked forever, and rendered as
        # a phantom "running 0/0" batch by any attached SSE stream). The Lua EXISTS+HSET is one
        # round trip, so a reaped key stays reaped instead of being recreated without an expiry.
        persist_sort = _get_persist_sort_script(redis_client)
        await persist_sort(keys=[key], args=[sort_state.key, sort_state.order], client=redis_client)
        try:
            dispatch_summary: list[dict[str, object]] = json.loads(data.get("dispatch_summary", "[]"))
        except json.JSONDecodeError:
            dispatch_summary = []
        agents_view = _sort_agents_view(_agents_view_from_hash(data, dispatch_summary), sort_state)

    return templates.TemplateResponse(
        request=request,
        name="execution/partials/agents_table.html",
        context={"agents": agents_view, "sort": sort_state, "batch_id": str(batch_id)},
    )


async def build_audit_log_context(
    session: AsyncSession,
    *,
    status: str | None,
    page: int,
    page_size: int,
    sort: str | None,
    order: str | None,
) -> dict[str, Any]:
    """Build every context key the audit log content needs, from the resolved view inputs alone.

    Extracted (phaze-uvmcr.3) so BOTH producers of "what the audit log pane looks like" derive it
    identically: the ``GET /audit/`` fragment endpoint below (the filter tab / pager / column-sort
    swap target -- unchanged in path and behavior) and ``shell._render_stage``'s ``"audit"``
    branch (the ``/s/audit`` direct-nav + rail-swap host, where ``GET /audit/`` now redirects a
    full-page request). Two independently-drifting descriptions of this content is exactly the
    phaze-7j50 class of defect ``build_propose_list_context`` (``routers/shell.py``) already paid
    to avoid for the propose workspace; this follows the same discipline.

    Does NOT include ``request`` -- callers merge that (and any stage-shell keys) into the base
    context themselves, the same split ``build_propose_list_context`` uses.
    """
    # phaze-a6hm.5: resolve BEFORE the read, same as every other sortable table (column_sort.py
    # USING IT). ``status`` rides view_state so a header click keeps the operator on their active
    # filter tab (contract rule 4); ``page`` deliberately does not -- a re-sort returns to page 1.
    sort_state = AUDIT_SORT.resolve(sort=sort, order=order, view_state={"status": status, "page_size": page_size})
    audit_page = await get_execution_logs_page(session, status=status, page=page, page_size=page_size, sort=sort_state)
    stats = await get_execution_stats(session)

    # phaze-37i1.2 (scope per owner decision after the phaze-37i1.1 diagnosis): an audit log that
    # has NEVER recorded anything is not the same page as one filtered down to nothing, and the
    # old shared "No operations recorded" copy read as a bug in both cases. ``execution_log`` is
    # written only for an EXECUTED proposal, and proposal generation is a deliberate,
    # LLM-costing, operator-triggered step -- so a zero here usually means "the archive has not
    # reached the propose stage yet", not "the query is broken". The never-run branch says so and
    # points at the convergence-gate backlog; ``count_proposal_pending_files`` is awaited ONLY on
    # that branch so the normal, populated page pays no extra query.
    proposal_ready_count = await count_proposal_pending_files(session) if stats["total"] == 0 else 0

    return {
        "logs": audit_page.rows,
        "pagination": audit_page,
        "stats": stats,
        "current_status": status or "all",
        "current_page": "audit",
        "sort": sort_state,
        "proposal_ready_count": proposal_ready_count,
    }


@router.get("/audit/", response_class=HTMLResponse)
async def audit_log(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    sort: str | None = Query(None),
    order: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the audit-content fragment for a live htmx swap; redirect everything else to /s/audit.

    phaze-uvmcr.3: "audit" is now a registered ``UTILITY_PANES`` shell stage
    (``routers/shell.py``), reachable with the rail intact at ``/s/audit`` -- which composes the
    SAME ``execution/audit_log.html`` content (now a content-only fragment, no ``{% extends %}``)
    this route used to render standalone via its own ``base.html`` fork. That full-page branch was
    therefore redundant, same shape as ``pipeline.pipeline_files``'s phaze-uvmcr.2 fix: a plain
    request, a bookmark/reload, or a history restore (``response_shape.wants_fragment`` is False
    for all three, per contract rule 2 -- a restore ignores ``hx-target`` and swaps into ``<body>``,
    so it is a full-document request too) now 301-redirects to ``/s/audit``, carrying the request's
    WHOLE query string across so a bookmarked/filtered ``/audit/`` URL still reads the same on the
    other side.

    The live htmx swap branch -- the filter tabs, the pager, and the column-sort headers, which all
    target ``#audit-content`` and hit THIS route unchanged -- is untouched: it still returns the
    chrome-less ``execution/partials/audit_content.html`` fragment, now sourced from the shared
    :func:`build_audit_log_context` so it cannot drift from ``/s/audit``'s own render of the same
    view. ``GET /audit/{log_id}/detail`` is a separate route entirely and is unaffected by any of
    this -- it is a fragment endpoint, not a bookmarkable page, and stays exactly where it is.
    """
    if not wants_fragment(request):
        # phaze-r6e5m (response_shape.py contract rule 6): this same URL also answers with the
        # fragment below depending on request headers alone, so the redirect must be as
        # browser-uncacheable as the fragment is -- otherwise a cached 301 (redirects are
        # heuristically cacheable by default) could be replayed for a live htmx swap that wanted
        # the fragment, or vice versa.
        query = request.url.query
        return RedirectResponse(url=f"/s/audit?{query}" if query else "/s/audit", status_code=301, headers=DUAL_SHAPE_RESPONSE_HEADERS)

    context: dict[str, Any] = {
        "request": request,
        **(await build_audit_log_context(session, status=status, page=page, page_size=page_size, sort=sort, order=order)),
    }
    return templates.TemplateResponse(
        request=request, name="execution/partials/audit_content.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS
    )


@router.get("/audit/{log_id}/detail", response_class=HTMLResponse)
async def audit_log_detail(
    request: Request,
    log_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the expanded per-entry detail row for one audit-log entry (phaze-37i1.3).

    "An audit log that lists events you cannot inspect has not solved the operator's problem"
    (phaze-37i1 epic) -- this is the drill-down: which file, which proposal it executed, and
    for a failed entry, its actual error text rather than the bare "Failed" badge the table row
    already shows.
    """
    log_entry = await get_execution_log_detail(session, log_id)
    if log_entry is None:
        raise HTTPException(status_code=404, detail="Audit log entry not found")
    return templates.TemplateResponse(
        request=request,
        name="execution/partials/audit_detail_row.html",
        context={"request": request, "log": log_entry},
    )
