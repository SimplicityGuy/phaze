"""Compute-lane detail pane route."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from phaze.database import get_session
from phaze.routers.pipeline._common import router, templates
from phaze.services.backends import LANE_RECENT_N, get_backend_lane_snapshot, get_lane_queue_depths, get_lane_recent_completions


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@router.get("/pipeline/lanes/{backend_id}", response_class=HTMLResponse)
async def lane_detail(
    request: Request,
    backend_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the lane-detail body fragment for a backend lane (DRILL-01 / D-06 / D-07 / D-00b).

    Swapped as innerHTML into the shared ``#detail-pane`` (88-01 shell). ``backend_id`` is operator-
    declared, so it is resolved by lookup-in-known-set against the degrade-safe
    :func:`get_backend_lane_snapshot` (T-88-03): an unknown/offline id renders the friendly "Lane
    offline" empty fragment (200, HTML -- never a 500/JSON/HTTPException, never a raw-param-driven read),
    so htmx still swaps a body into the pane. For a resolved lane the kind-adaptive body renders the
    last ``LANE_RECENT_N`` newest-first completions -- ALL lane kinds, including local (phaze-2u8v.3;
    D-07) -- and the per-lane queue depths; every read is bounded + degrade-safe (D-00b) and the body
    carries its own bounded 5s tick (D-03). Read-only -- no commit. Only secret-free filename/timestamp/id
    scalars leave here (T-88-04); ``backend_id``/``kind`` stay Jinja-autoescaped (T-88-05).
    """
    lanes = await get_backend_lane_snapshot(session, request.app.state)  # degrade-safe -> []
    lane = next((one for one in lanes if one["id"] == backend_id), None)
    if lane is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_lane_detail.html",
            context={
                "lane": None,
                "backend_id": backend_id,
                "recent_completions": [],
                "queue_depths": {},
                "queue_depths_agent": None,
                "queue_depths_note": None,
                "refreshed_at": None,
                "recent_n": LANE_RECENT_N,
            },
        )
    recent_completions = await get_lane_recent_completions(session, backend_id, lane["kind"])
    # phaze-2u8v.1: the depths are read off the AGENT this lane's dispatch actually enqueues to (a local
    # lane -> the live fileserver agent), never off the registry id. ``depths is None`` is "this lane has
    # no SAQ queue", carried to the template as a NOTE -- the template must not render it as zeros.
    queue_depths = await get_lane_queue_depths(session, request.app.state, backend_id, lane["kind"])
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/_lane_detail.html",
        context={
            "lane": lane,
            "backend_id": backend_id,
            "recent_completions": recent_completions,
            "queue_depths": queue_depths.depths,
            "queue_depths_agent": queue_depths.agent_id,
            "queue_depths_note": queue_depths.note,
            "refreshed_at": datetime.now(UTC),
            "recent_n": LANE_RECENT_N,
        },
    )
