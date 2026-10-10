"""Controller-only bounded companion import continuation; owning agents perform reads."""

import time
from typing import Any
import uuid

from sqlalchemy import select

from phaze.models.companion_import import CompanionImportRun
from phaze.services.companion_autolink import agent_association_lock, run_agent_association, scan_running
from phaze.services.companion_import_backfill import process_run_page


async def enqueue_import_run(queue: Any, run_id: uuid.UUID, *, step: uuid.UUID, delayed: bool = False) -> None:
    """The durable continuation identity survives broker acknowledgement loss/replay."""
    await queue.enqueue("import_agent_companions", run_id=str(run_id), step=str(step), scheduled=time.time() + (10 if delayed else 0))


async def _continue(ctx: dict[str, Any], identity: uuid.UUID, previous: uuid.UUID) -> None:
    async with ctx["async_session"].begin() as session:
        run = await session.scalar(select(CompanionImportRun).where(CompanionImportRun.id == identity).with_for_update())
        if run is None:
            raise ValueError("Unknown companion import run")
        if run.continuation == previous:
            run.continuation = uuid.uuid4()
        next_step = run.continuation
    await enqueue_import_run(ctx["queue"], identity, step=next_step, delayed=True)


async def import_agent_companions(ctx: dict[str, Any], *, run_id: str, step: str) -> dict[str, Any]:
    """One source page; no request thread or scan/watcher waits for archive reads."""
    identity, requested = uuid.UUID(run_id), uuid.UUID(step)
    async with ctx["async_session"]() as session:
        run = await session.get(CompanionImportRun, identity)
        if run is None:
            raise ValueError("Unknown companion import run")
        agent_id, associated, current = run.agent_id, run.associated, run.continuation
    if requested != current:
        await enqueue_import_run(ctx["queue"], identity, step=current, delayed=True)
        return {"run_id": run_id, "status": "continuation_replayed"}
    if not associated:
        deferred = False
        async with ctx["async_session"]() as session:
            if await scan_running(session, agent_id):
                deferred = True
            else:
                async with agent_association_lock(session, agent_id) as acquired:
                    if not acquired:
                        deferred = True
                    else:
                        await run_agent_association(session, agent_id, apply=True)
        if deferred:
            await _continue(ctx, identity, requested)
            return {"run_id": run_id, "status": "deferred"}
        async with ctx["async_session"].begin() as session:
            run = await session.scalar(select(CompanionImportRun).where(CompanionImportRun.id == identity).with_for_update())
            if run is not None:
                run.associated = True
    status = await process_run_page(ctx["async_session"], ctx["task_router"], identity)
    if not status["complete"]:
        await _continue(ctx, identity, requested)
    return status
