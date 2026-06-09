"""Execution UI router -- execute button, SSE progress, and audit log.

Phase 28 D-09 + D-11 rewrite: ``start_execution`` now fans out approved
proposals by ``FileRecord.agent_id``, chunks each group at 500, seeds the
``exec:{batch_id}`` Redis hash (D-04), and enqueues one sub-job per
(agent, chunk) via ``AgentTaskRouter.enqueue_for_agent``. ``execution_progress``
emits three SSE event types every tick (``progress``, ``agents_table``,
plus a one-shot ``dispatch_summary`` on first connect) and closes on either
``complete`` or ``complete_with_errors``.

The application server is the sole writer of the ``exec:{batch_id}`` hash via
HSET at dispatch; HINCRBY mutations come exclusively from the Plan 28-02 POST
endpoint (``routers/agent_exec_batches.py``). Both writers use
``app.state.redis`` (decode_responses=True) so the SSE reader gets ``str``,
not ``bytes``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse
import structlog

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.collision import detect_collisions
from phaze.services.execution_dispatch import (
    chunk_proposals,
    count_revoked_skipped_proposals,
    get_approved_proposals_grouped_by_agent,
)
from phaze.services.execution_queries import get_execution_logs_page, get_execution_stats


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["execution"])


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


@router.post("/execution/start", response_class=HTMLResponse)
async def start_execution(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Dispatch approved proposals as per-agent SAQ sub-jobs (Phase 28 D-09).

    Sequence:
      1. Pre-check collisions (unchanged from Phase 25) -- destinations collide
         GLOBALLY, not per-agent, so the check fires before any grouping.
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
    # 1. Pre-check collision (unchanged) -- collision_block short-circuits dispatch.
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
    if groups:
        # Only seed when there is at least one (agent, chunk) to dispatch. An
        # empty hash with status="running" and TTL would mislead the SSE reader.
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(f"exec:{batch_id}", mapping=init_fields)
            pipe.expire(f"exec:{batch_id}", 86400)
            await pipe.execute()

    # 6. Per-(agent, chunk) enqueue. Log-and-continue on individual failures
    # (PATTERNS S5) so a single bad enqueue does not kill the whole dispatch.
    task_router = request.app.state.task_router
    for agent_id, items in groups.items():
        for chunk_index, chunk in enumerate(chunk_proposals(items)):
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
            except Exception:
                logger.exception(
                    "dispatch: enqueue failed for agent=%s chunk=%s batch_id=%s",
                    agent_id,
                    chunk_index,
                    batch_id,
                )

    # 7. D-11 dispatch INFO log.
    logger.info(
        "dispatch batch_id=%s total=%d n_agents=%d subjobs_expected=%d",
        batch_id,
        total,
        len(groups),
        subjobs_expected,
    )

    # 8. First-render context for progress.html.
    return templates.TemplateResponse(
        request=request,
        name="execution/partials/progress.html",
        context={
            "request": request,
            "batch_id": str(batch_id),
            "skipped_revoked": skipped_revoked,
            "total": total,
            "completed": 0,
            "failed": 0,
            "subjobs_expected": subjobs_expected,
            "agents": _build_agents_view(groups, agent_names=agent_names),
            "status": "running",
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
async def execution_progress(request: Request, batch_id: str) -> EventSourceResponse:
    """Stream SSE events with real-time execution progress from Redis (D-04 + D-11).

    Event sequence per poll tick (1s cadence):
      - ``dispatch_summary`` (ONCE, on first connect with non-empty hash) --
        rendered HTML of the heading line.
      - ``progress`` (every tick) -- rendered HTML of the aggregate counter row.
      - ``agents_table`` (every tick) -- rendered HTML of the per-agent table.

    On terminal status (``complete`` or ``complete_with_errors``) the generator
    yields the final ``progress`` + ``agents_table`` events for that state,
    then emits the matching close event and returns.
    """
    redis_client = request.app.state.redis

    async def event_generator() -> AsyncGenerator[dict[str, str]]:
        first_connect = True
        while True:
            data: dict[str, str] = await redis_client.hgetall(f"exec:{batch_id}")
            if not data:
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

            # Every-tick agents_table event (UI-SPEC C2).
            agents_html = _render_partial(
                request,
                "execution/partials/agents_table.html",
                {"agents": agents_view},
            )
            yield {"event": "agents_table", "data": agents_html}

            # Terminal status: close on either complete OR complete_with_errors
            # (CONTEXT specifics line 264 widens the existing single-status check).
            if status in {"complete", "complete_with_errors"}:
                if failed == 0:
                    msg = f'Execution complete. All {total} files renamed successfully. <a href="/audit/" class="text-blue-600 hover:underline ml-2">View Audit Log</a>'
                else:
                    msg = f'Execution complete. {completed} succeeded, {failed} failed. <a href="/audit/" class="text-blue-600 hover:underline ml-2">View Audit Log</a>'
                yield {"event": status, "data": msg}
                return

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())


@router.get("/audit/", response_class=HTMLResponse)
async def audit_log(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the audit log page, or an HTMX table fragment."""
    logs, pagination = await get_execution_logs_page(session, status=status, page=page, page_size=page_size)
    stats = await get_execution_stats(session)

    context = {
        "request": request,
        "logs": logs,
        "pagination": pagination,
        "stats": stats,
        "current_status": status or "all",
        "current_page": "audit",
    }

    # HTMX requests get tabs + table fragment (so tab active state updates)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="execution/partials/audit_content.html", context=context)

    return templates.TemplateResponse(request=request, name="execution/audit_log.html", context=context)
