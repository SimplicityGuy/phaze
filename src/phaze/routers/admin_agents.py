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
from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.routers.response_shape import wants_fragment
from phaze.services.agent_liveness import classify, derive_compute_lane_identities, non_local_backend_agent_refs, non_local_backend_kinds, sort_key
from phaze.services.pipeline import _agent_stage_buckets, get_agent_lane_depths, get_agent_recent_scans
from phaze.utils.humanize import relative_time


# The six pipeline stages surfaced in the agent-activity COUNT matrix (DRILL-02 / D-04). TRACKLIST is
# OMITTED (the 7-stage -> 6-pill remap, RESEARCH Pitfall 3); REVIEW renders under "Appr" and APPLY under
# "Exec" in the template. Ordered for the matrix's left-to-right column order.
_ACTIVITY_STAGES: tuple[Stage, ...] = (Stage.METADATA, Stage.FINGERPRINT, Stage.ANALYZE, Stage.PROPOSE, Stage.REVIEW, Stage.APPLY)


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Expose the relative-time helper to all templates rendered through this
# router. The agents_table partial uses it via {{ humanize_relative_time(...) }}.
templates.env.globals["humanize_relative_time"] = relative_time

router = APIRouter(prefix="/admin/agents", tags=["admin"])


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

    COMPUTE-01 (dedupe): a bearer-token ``kind='compute'`` Agent row whose id/name matches a
    non-local registry backend is the SAME cluster now surfaced as a live tile in Section 2. Such a
    row never heartbeats (it is not a persistent process), so it would otherwise sit ``NEVER`` forever
    in Section 1 while its lane is live in Section 2 — the "shown twice" defect. We suppress ONLY that
    exact shadow: ``kind=='compute'`` AND (``id`` or ``name``) is a registry backend key OR a registry
    ``agent_ref`` AND ``_status=='never'``. The predicate is deliberately narrow — a genuinely
    heartbeating compute agent keeps its row, a non-registry NEVER compute row keeps its row, and
    fileserver rows are untouched. Display-only (mirrors the revoked-row filter): no DB mutation, no
    schema change. Reading the registry is degrade-safe — a settings failure leaves every row visible
    rather than raising into the hot poll.

    phaze-ifcr: id/name string equality against the backend's OWN id (``registry_keys``) misses whenever
    the operator's callback-agent id/name diverges from the backend id it dispatches for — e.g. a kueue
    backend id ``"vox"`` bound to callback agent ``"k8s-vox"`` (``phaze agents add --kind compute``).
    ``non_local_backend_agent_refs`` closes that gap with the STRUCTURAL binding (``agent_ref``) instead
    of name-coincidence, so the shadow row is suppressed regardless of what the operator named it.
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
    try:
        settings = get_settings()
        registry_keys = set(non_local_backend_kinds(settings))  # type: ignore[arg-type]
        registry_agent_refs = set(non_local_backend_agent_refs(settings))  # type: ignore[arg-type]
    except Exception:
        # A registry/settings read failure must never break the operator poll: leave every row
        # visible (the shadow row reappears, but the page still renders) rather than raise.
        logger.warning("agents_registry_shadow_filter_unavailable", exc_info=True)
        registry_keys = set()
        registry_agent_refs = set()
    shadow_keys = registry_keys | registry_agent_refs
    rows = [
        a
        for a in rows
        if not (a.kind == "compute" and a._status == "never" and (a.id in shadow_keys or a.name in shadow_keys))  # type: ignore[attr-defined]
    ]
    rows.sort(key=lambda a: sort_key(a, now))
    return rows, now


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render either the full ``admin/agents.html`` page or the partial.

    Shape decided by ``response_shape.wants_fragment`` (contract rule 1): a LIVE htmx swap
    gets only the partial so the response stays under a kilobyte. Otherwise the dedicated
    ``/_table`` route is the canonical polling target — this page handler exists primarily
    for the first-load full-page render and direct navigation.

    phaze-64uy replaced a local ``_is_htmx`` helper here, which re-derived the decision from
    the raw ``HX-Request`` header (banned by contract rule 1) and so got the restore case
    wrong. ``admin/partials/agents_table.html`` sets ``hx-push-url="/admin/agents?agent=<id>"``
    on each drill-in row, so that URL enters history; a Back with the snapshot evicted arrives
    here as a restore carrying BOTH headers, and htmx swaps a restore into ``<body>`` while
    ignoring ``hx-target``. The old helper answered that with the chrome-less table partial,
    replacing the whole admin page with a bare table. A restore now gets ``admin/agents.html``.
    """
    agents, now = await _load_agents(session)
    # Section 2 (RECORD-03 / D-07 → COMPUTE-01): one ephemeral compute-lane identity PER non-local
    # registry backend, synthesized read-only from the Phase-67 registry + in-flight CloudJob counts.
    # Injected on BOTH the full page and the partial so the existing 5s self-poll refreshes it too
    # (no new loop, RESEARCH OQ-1).
    compute_lanes = await derive_compute_lane_identities(session)
    template = "admin/partials/agents_table.html" if wants_fragment(request) else "admin/agents.html"
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "request": request,
            "agents": agents,
            "now": now,
            "current_page": "admin_agents",
            "refreshed_at_iso": now.isoformat(),
            "compute_lanes": compute_lanes,
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
    compute_lanes = await derive_compute_lane_identities(session)
    return templates.TemplateResponse(
        request=request,
        name="admin/partials/agents_table.html",
        context={
            "request": request,
            "agents": agents,
            "now": now,
            "refreshed_at_iso": now.isoformat(),
            "compute_lanes": compute_lanes,
            "selected_agent": _resolve_selected_agent(agent, agents),
        },
    )


@router.get("/{agent_id}/_activity", response_class=HTMLResponse)
async def agent_activity(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent_id: str,
) -> HTMLResponse:
    """Render the agent-activity body fragment swapped into the shared ``#detail-pane`` shell (DRILL-02).

    The wave-2 body for plan 88-01's non-modal detail pane: the agent-row trigger's
    ``hx-get="/admin/agents/{id}/_activity"`` innerHTML-swaps THIS fragment into ``#detail-pane``, and
    the fragment carries its OWN bounded ``hx-trigger="every 5s"`` self-refresh (D-03) — so the endpoint
    returns the body directly, NOT the shell (the 88-01 shell is a static host with no body slot).

    For a found agent the fragment stacks (D-05): liveness header (``classify`` transient ``_status`` +
    kind badge + last-seen) → a per-agent 6-stage COUNT matrix (one indexed ``GROUP BY stage_status_case``
    per stage scoped to ``agent_id`` via :func:`_agent_stage_buckets`, D-04/D-00a) → per-lane queue depths
    → recent scan batches. Every read is bounded + degrade-safe (D-00b): an unknown ``agent_id`` renders
    a friendly empty fragment at **200** (mirroring the sibling ``lane_detail`` never-error posture, T-88-07)
    — NEVER an ``HTTPException`` / JSON / 500 — and an agent owning 0 files renders the "owns no files yet"
    empty state. The 200 (not 404) is load-bearing: the ``/admin/agents`` page has no htmx 404 swap opt-in
    (its ``htmx:responseError`` handler targets ``#agents-table-section``, not ``#detail-pane``), so a 404
    fragment would be DISCARDED — the pane would keep stale content and the self-poll would 404-loop forever
    on an agent revoked mid-view (WR-01/WR-02). The not-found fragment carries NO own-tick, so returning it
    at 200 terminates the poll loop cleanly.

    Read-only: ``get_session`` never commits and this handler issues no writes (T-88-10 — it reads ONLY
    the derived ``stage_status_case``, never ``FileRecord.state``).
    """
    now = datetime.now(UTC)
    agent = await session.get(Agent, agent_id)
    if agent is None:
        # Friendly empty fragment (T-88-07 IDOR guard): a raw/unknown/hostile id renders a benign body,
        # never a raw-param-driven 500. Returned at 200 (WR-01) — the /admin/agents page has no htmx 404
        # swap opt-in, so a 404 would be discarded, leaving stale pane content and a 404-looping self-poll
        # on a revoked-mid-view agent. The fragment carries no own-tick, so 200 terminates the poll cleanly.
        return templates.TemplateResponse(
            request=request,
            name="admin/partials/_agent_activity.html",
            context={"request": request, "agent": None, "now": now},
        )

    agent._status = classify(agent, now)  # type: ignore[attr-defined]  # Phase 27 transient-attr pattern
    # D-04: a per-agent 6-stage matrix of COUNTS — one indexed GROUP BY per stage, keyed by Stage VALUE
    # ('metadata'..'apply') so the template's Appr=review / Exec=apply remap resolves (RESEARCH Pitfall 3).
    buckets = {stage.value: await _agent_stage_buckets(session, agent_id, stage) for stage in _ACTIVITY_STAGES}
    queue_depths = await get_agent_lane_depths(request.app.state, agent_id)
    recent_scans = await get_agent_recent_scans(session, agent_id)
    return templates.TemplateResponse(
        request=request,
        name="admin/partials/_agent_activity.html",
        context={
            "request": request,
            "agent": agent,
            "now": now,
            "buckets": buckets,
            "queue_depths": queue_depths,
            "recent_scans": recent_scans,
            "refreshed_at_iso": now.isoformat(),
        },
    )
