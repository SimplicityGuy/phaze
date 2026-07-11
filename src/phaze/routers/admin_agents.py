"""GET /admin/agents (full page) + GET /admin/agents/_table (HTMX partial) — Phase 29 D-11..D-14.

Operator-facing read-only admin page that lists every non-revoked registered
file-server agent with a status pill (alive/stale/dead/never). Revoked agents
are filtered out entirely (see ``_load_agents``). The page polls
``/admin/agents/_table`` every 5 seconds via an HTMX self-replacing
``<section>`` (UI-SPEC §Polling LOCKED — never halts).

Server-side classification: ``_load_agents`` queries non-revoked Agent rows
(``revoked_at IS NULL``), computes ``classify(a, now)`` for each, injects the
result on a transient ``agent._status`` attribute (mirrors Phase 27's
``_agent_name`` / ``_elapsed_seconds`` pattern), and sorts via ``sort_key`` so
agents sort alive→stale→dead→never with last_seen DESC tiebreakers. (``classify``
/ ``sort_key`` retain their revoked tier for callers elsewhere, but revoked
agents never reach this panel.)

Auth posture: NO ``get_authenticated_agent`` dependency — operator pages are
open on the private LAN (consistent with pipeline.py / pipeline_scans.py
precedent; CONTEXT.md D-discretion).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 — FastAPI needs runtime import to resolve Annotated[AsyncSession, Depends(...)]
import structlog

from phaze.config import get_settings
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


def _resolve_selected_agent(agent: str | None, agents: list[Agent]) -> str | None:
    """Resolve the pushed ``?agent=`` id by lookup-in-known-set (DRILL-03 / D-02, T-88-01).

    Returns the id only when it names a currently-loaded agent, so an unknown/absent/hostile id
    highlights nothing and can never reach a template as a trusted value. Kept ASCII/autoescaped.
    """
    return agent if agent is not None and any(a.id == agent for a in agents) else None


async def _load_agents(session: AsyncSession) -> tuple[list[Agent], datetime]:
    """Load non-revoked Agents, attach transient ``_status``, sort per UI-SPEC LOCKED.

    Revoked agents (``revoked_at IS NOT NULL``) are excluded via the shared
    ``Agent.revoked_at.is_(None)`` convention used across the codebase
    (main.py / shell.py / pipeline.py) — this suppresses the permanently-revoked
    ``legacy-application-server`` FK-placeholder row (and any other revoked
    agent) from the operator panel. The row still exists in the DB; only its
    display here is filtered.

    Returns ``(agents, now)``. The ``now`` value is captured ONCE so the
    classify/sort step AND the template's ``refreshed_at_iso`` reflect the
    same instant — eliminates a few-microsecond skew that would otherwise
    show up if the template re-evaluated ``datetime.now(UTC)`` separately.
    """
    result = await session.execute(select(Agent).where(Agent.revoked_at.is_(None)))
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
    agent: Annotated[str | None, Query()] = None,
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
            # Phase 88 (88-01, DRILL-03 / D-02): seed the selected-agent highlight from ?agent= so a
            # reload re-opens the row selection; the self-poll re-applies it thereafter. Lookup-in-
            # known-set (T-88-01) — unknown/absent id highlights nothing, never errors.
            "selected_agent": _resolve_selected_agent(agent, agents),
            "enable_saq_ui": get_settings().enable_saq_ui,  # CLEAN-01: gate the discreet /saq footer link (presentation-only)
        },
    )


@router.get("/_table", response_class=HTMLResponse)
async def table_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Return the agents_table partial UNCONDITIONALLY.

    This is the HTMX poll target. The partial re-emits its own
    ``hx-trigger="every 5s"`` attribute so the next tick fires automatically
    after the outerHTML swap (UI-SPEC §Polling LOCKED — never halts).

    Phase 88 (88-01, DRILL-03 / D-02): the ``#agents-table-section`` self-poll carries the pushed
    ``?agent=`` via ``hx-vals`` (agents_table.html), so this tick re-emits the selected-row highlight
    (aria-current + ring) on the matching row. Resolved by lookup-in-known-set (T-88-01) — an
    unknown/absent id highlights nothing, never a 422/500 into the poll.
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
            "selected_agent": _resolve_selected_agent(agent, agents),
        },
    )
