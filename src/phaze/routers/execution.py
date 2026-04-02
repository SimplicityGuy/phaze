"""Execution UI router -- execute button, SSE progress, and audit log."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from phaze.database import get_session
from phaze.services.collision import detect_collisions
from phaze.services.execution_queries import get_execution_logs_page, get_execution_stats


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["execution"])


@router.post("/execution/start", response_class=HTMLResponse)
async def start_execution(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Trigger batch execution of all approved proposals via SAQ.

    Returns a collision block if duplicate destination paths exist among
    approved proposals, preventing execution until collisions are resolved.
    """
    collisions = await detect_collisions(session)
    if collisions:
        return templates.TemplateResponse(
            request=request,
            name="execution/partials/collision_block.html",
            context={"request": request, "collisions": collisions},
        )

    queue = request.app.state.queue
    batch_id = uuid4().hex
    await queue.enqueue("execute_approved_batch", batch_id=batch_id)
    return templates.TemplateResponse(
        request=request,
        name="execution/partials/progress.html",
        context={"request": request, "batch_id": batch_id},
    )


@router.get("/execution/progress/{batch_id}")
async def execution_progress(request: Request, batch_id: str) -> EventSourceResponse:
    """Stream SSE events with real-time execution progress from Redis."""
    queue = request.app.state.queue

    async def event_generator() -> AsyncGenerator[dict[str, str]]:
        while True:
            data = await queue.redis.hgetall(f"exec:{batch_id}")
            if not data:
                yield {"event": "progress", "data": "Waiting for execution to start..."}
            else:
                # Redis returns bytes; decode values
                decoded = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v for k, v in data.items()}
                total = int(decoded.get("total", 0))
                completed = int(decoded.get("completed", 0))
                failed = int(decoded.get("failed", 0))
                status = decoded.get("status", "running")

                if status == "complete":
                    if failed == 0:
                        msg = f'Execution complete. All {total} files renamed successfully. <a href="/audit/" class="text-blue-600 hover:underline ml-2">View Audit Log</a>'
                    else:
                        succeeded = completed
                        msg = f'Execution complete. {succeeded} succeeded, {failed} failed. <a href="/audit/" class="text-blue-600 hover:underline ml-2">View Audit Log</a>'
                    yield {"event": "complete", "data": msg}
                    return

                msg = f"{completed}/{total} files processed ({failed} failed)" if failed > 0 else f"{completed}/{total} files processed"
                yield {"event": "progress", "data": msg}

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
