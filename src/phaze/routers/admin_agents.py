"""GET /admin/agents (full page) + GET /admin/agents/_table (HTMX partial) — Phase 29 D-11..D-14.

Operator-facing read-only admin page that lists every registered file-server
agent with a 5-state status pill (alive/stale/dead/revoked/never). The page
polls ``/admin/agents/_table`` every 5 seconds via an HTMX self-replacing
``<section>`` (UI-SPEC §Polling LOCKED — never halts).

Server-side classification: ``_load_agents`` queries Agent rows, computes
``classify(a, now)`` for each, injects the result on a transient
``agent._status`` attribute (mirrors Phase 27's ``_agent_name`` /
``_elapsed_seconds`` pattern), and sorts via ``sort_key`` so revoked agents
land last and non-revoked agents sort alive→stale→dead→never with
last_seen DESC tiebreakers.

Auth posture: NO ``get_authenticated_agent`` dependency — operator pages are
open on the private LAN (consistent with pipeline.py / pipeline_scans.py
precedent; CONTEXT.md D-discretion).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 — FastAPI needs runtime import to resolve Annotated[AsyncSession, Depends(...)]
import structlog

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.services.agent_liveness import classify, classify_compute_lanes, sort_key
from phaze.utils.humanize import relative_time


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Expose the relative-time helper to all templates rendered through this
# router. The agents_table partial uses it via {{ humanize_relative_time(...) }}.
templates.env.globals["humanize_relative_time"] = relative_time

router = APIRouter(prefix="/admin/agents", tags=["admin"])


def _is_htmx(request: Request) -> bool:
    """Return True if the request has the HTMX-set ``HX-Request: true`` header.

    Matches the project-wide pattern documented in STATE.md
    ("Search UI: HTMX partial detection via truthy HX-Request header check").
    """
    return request.headers.get("hx-request") == "true"


async def _load_agents(session: AsyncSession) -> tuple[list[Agent], datetime]:
    """Load every Agent, attach transient ``_status``, sort per UI-SPEC LOCKED.

    Returns ``(agents, now)``. The ``now`` value is captured ONCE so the
    classify/sort step AND the template's ``refreshed_at_iso`` reflect the
    same instant — eliminates a few-microsecond skew that would otherwise
    show up if the template re-evaluated ``datetime.now(UTC)`` separately.
    """
    result = await session.execute(select(Agent))
    rows = list(result.scalars().all())
    now = datetime.now(UTC)
    for a in rows:
        # Transient ORM attribute injection (Phase 27 pattern). SQLAlchemy
        # ignores attrs not declared as Mapped columns, so this stays out of
        # the DB and is safe even if a future migration adds a real
        # ``_status`` column (Python's leading-underscore convention).
        a._status = classify(a, now)  # type: ignore[attr-defined]  # Phase 27 transient-attr pattern
    rows.sort(key=lambda a: sort_key(a, now))
    return rows, now


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Render either the full ``admin/agents.html`` page or the partial.

    HX-Request-aware: when the operator's HTMX client sends ``HX-Request: true``
    (most polling clients do), we return only the partial so the response
    stays under a kilobyte. Otherwise the dedicated ``/_table`` route is
    the canonical polling target — this page handler exists primarily for
    the first-load full-page render and direct navigation.
    """
    agents, now = await _load_agents(session)
    # Section 2 (RECORD-03 / D-07): the ephemeral k8s burst-lane liveness, synthesized
    # read-only from in-flight CloudJob counts. Injected on BOTH the full page and the
    # partial so the existing 5s self-poll refreshes it too (no new loop, RESEARCH OQ-1).
    compute_lane_state, compute_lane_count = await classify_compute_lanes(session)
    template = "admin/partials/agents_table.html" if _is_htmx(request) else "admin/agents.html"
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "request": request,
            "agents": agents,
            "now": now,
            "current_page": "admin_agents",
            "refreshed_at_iso": now.isoformat(),
            "compute_lane_state": compute_lane_state,
            "compute_lane_count": compute_lane_count,
        },
    )


@router.get("/_table", response_class=HTMLResponse)
async def table_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Return the agents_table partial UNCONDITIONALLY.

    This is the HTMX poll target. The partial re-emits its own
    ``hx-trigger="every 5s"`` attribute so the next tick fires automatically
    after the outerHTML swap (UI-SPEC §Polling LOCKED — never halts).
    """
    agents, now = await _load_agents(session)
    compute_lane_state, compute_lane_count = await classify_compute_lanes(session)
    return templates.TemplateResponse(
        request=request,
        name="admin/partials/agents_table.html",
        context={
            "request": request,
            "agents": agents,
            "now": now,
            "refreshed_at_iso": now.isoformat(),
            "compute_lane_state": compute_lane_state,
            "compute_lane_count": compute_lane_count,
        },
    )
