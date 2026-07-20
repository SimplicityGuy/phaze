"""GET /admin/agents (full page) + GET /admin/agents/_table (HTMX partial) — Phase 29 D-11..D-14.

Operator-facing read-only admin page that lists every non-revoked registered
file-server agent with a status pill (alive/stale/dead/never). Revoked agents
are filtered out entirely (see ``_load_agents``). The page polls
``/admin/agents/_table`` every 5 seconds via an HTMX self-replacing
``<section>`` (UI-SPEC §Polling LOCKED — never halts).

Server-side classification: ``_load_agents`` queries non-revoked Agent rows
(``revoked_at IS NULL``), computes ``classify(a, now)`` for each and injects the
result on a transient ``agent._status`` attribute (mirrors Phase 27's
``_agent_name`` / ``_elapsed_seconds`` pattern). Agents still render
alive→stale→dead→never with last_seen DESC tiebreakers by default. (``classify``
retains its revoked tier for callers elsewhere, but revoked agents never reach
this panel.)

Column sorting (phaze-a6hm.4): the ORDER BY now lands in SQL via :data:`AGENTS_SORT`
rather than a Python ``rows.sort(key=sort_key)`` after the read. The default
resolves to the SAME order ``sort_key`` produced — see :data:`AGENTS_SORT` for why
the two are equivalent once revoked rows are filtered, and
``test_default_matches_locked_sort_key`` for the pin that keeps them so.
``sort_key`` itself is unchanged and still serves its other callers.

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
from sqlalchemy import DateTime, Integer, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 — FastAPI needs runtime import to resolve Annotated[AsyncSession, Depends(...)]
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.routers.column_sort import DESCENDING, SortableColumn, SortContract, SortState
from phaze.routers.response_shape import wants_fragment
from phaze.services.agent_liveness import classify, derive_compute_lane_identities, non_local_backend_agent_refs, non_local_backend_kinds
from phaze.services.pipeline import _agent_stage_buckets, get_agent_lane_depths, get_agent_recent_scans
from phaze.utils.humanize import relative_time


# The six pipeline stages surfaced in the agent-activity COUNT matrix (DRILL-02 / D-04). TRACKLIST is
# OMITTED (the 7-stage -> 6-pill remap, RESEARCH Pitfall 3); REVIEW renders under "Appr" and APPLY under
# "Exec" in the template. Ordered for the matrix's left-to-right column order.
_ACTIVITY_STAGES: tuple[Stage, ...] = (Stage.METADATA, Stage.FINGERPRINT, Stage.ANALYZE, Stage.PROPOSE, Stage.REVIEW, Stage.APPLY)


logger = structlog.get_logger(__name__)


# ``last_seen_at`` with NULL folded to the OLDEST representable instant, so a direction alone decides
# where never-seen agents land and no caller needs a nullslast() the sort contract cannot express.
#
# This is load-bearing, not cosmetic. Postgres orders NULLS FIRST under DESC, so a bare
# ``Agent.last_seen_at.desc()`` puts every NEVER agent ABOVE the live ones — silently inverting the
# UI-SPEC LOCKED order on the default render. Folding to ``-infinity`` reproduces ``sort_key``'s
# "+inf tiebreaker → end of the bucket" exactly: DESC = most-recent first, never-seen last.
_LAST_SEEN_ORDER = func.coalesce(Agent.last_seen_at, cast(literal("-infinity"), DateTime(timezone=True)))

# Queue depth lives in the ``last_status`` JSONB blob and is absent for agents that never reported one
# (the template renders those as "—"). Folded to -1 so they sort BELOW every real depth under ASC and
# above nothing under DESC, rather than scattering on NULL ordering rules.
_QUEUE_DEPTH_ORDER = func.coalesce(cast(Agent.last_status["queue_depth"].astext, Integer), -1)

AGENTS_SORT = SortContract(
    endpoint="/admin/agents/_table",
    target="#agents-table-section",
    columns=(
        SortableColumn(key="name", label="Agent", expression=Agent.name),
        SortableColumn(key="kind", label="Kind", expression=Agent.kind),
        SortableColumn(key="queue", label="Queue", expression=_QUEUE_DEPTH_ORDER),
        SortableColumn(key="last_seen", label="Last seen", expression=_LAST_SEEN_ORDER),
        SortableColumn(key="scan_roots", label="Scan roots", expression=func.jsonb_array_length(Agent.scan_roots)),
    ),
    default_key="last_seen",
    default_order=DESCENDING,
)
"""The whitelist for the /admin/agents table (contract rule 6, built at import time).

``default_key="last_seen"`` + ``DESCENDING`` is not a new default — it is the UI-SPEC LOCKED order
``sort_key`` already produced, re-expressed as SQL. With revoked rows filtered out by ``_load_agents``
(they are the ONLY rows whose ``revoked_int`` differs), ``sort_key``'s tuple collapses to
``(status_rank, -last_seen)``, and ``status_rank`` is a pure monotone bucketing of last-seen recency:
alive = seen most recently, then stale, then dead, then never (``last_seen_at IS NULL``). Ordering by
``last_seen_at DESC NULLS LAST`` therefore yields the SAME total order, alive→stale→dead→never, with
the same within-bucket "most recently seen first" tiebreak. ``test_default_matches_locked_sort_key``
pins that equivalence so a future threshold change cannot drift the two apart unnoticed.

"Status" is deliberately NOT a separate key for the same reason: it would be a synonym for "Last seen"
whose caret pointed the opposite way (status ascending = last-seen descending), which reads as the
table having re-sorted itself in a direction the operator did not choose. "Actions" holds no data.

``resolve()`` is called with NO ``view_state``, which is a deviation from rule 4 worth stating so it is
not "corrected" later. This table's other view parameter is ``?agent=`` (the drill-in selection), and it
must NOT ride in ``view_state``, because ``view_state`` feeds :meth:`SortState.poll_url` and the poll is
exactly where a stale value does damage: a row click swaps only ``#detail-pane``, leaving THIS section's
server-rendered markup — and therefore its ``poll_url`` — holding the PREVIOUS selection. A baked
``?agent=`` would re-assert that stale id every 5 seconds and erase the ring the operator just clicked
(Phase 88 D-02). It travels in the section's ``hx-vals`` instead, read live from ``location.search``,
which htmx inherits to the sort buttons so a header click preserves the open pane too. The two channels
are kept disjoint because htmx APPENDS ``hx-vals`` onto the ``hx-get`` query string — a key in both
would be transmitted twice. See ``admin/partials/agents_table.html`` for the same note at the markup.
"""

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


async def _load_agents(session: AsyncSession, sort: SortState) -> tuple[list[Agent], datetime]:
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
    # ORDER BY lands in SQL (sort contract rule 1), reached only through a whitelisted expression.
    # Agent.id is the tiebreaker: an operator-chosen key ties heavily (every fileserver agent shares a
    # kind, most share a scan-root count), and without a unique tail those ties would re-shuffle between
    # 5s polls — rows visibly swapping places under a cursor that never moved.
    stmt = select(Agent).where(Agent.revoked_at.is_(None)).order_by(*sort.order_by(), Agent.id.asc())
    result = await session.execute(stmt)
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
    # No Python re-sort: the order arrived from SQL above. Filtering never reorders a list, so the
    # shadow-row removal preserves the ORDER BY rather than needing it re-applied.
    return rows, now


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
    order: Annotated[str | None, Query()] = None,
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
    sort_state = AGENTS_SORT.resolve(sort=sort, order=order)
    agents, now = await _load_agents(session, sort_state)
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
            "sort": sort_state,
            "enable_saq_ui": get_settings().enable_saq_ui,  # CLEAN-01: gate the discreet /saq footer link (presentation-only)
        },
    )


@router.get("/_table", response_class=HTMLResponse)
async def table_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent: Annotated[str | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
    order: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Return the agents_table partial UNCONDITIONALLY.

    This is the HTMX poll target. The partial re-emits its own
    ``hx-trigger="every 5s"`` attribute so the next tick fires automatically
    after the outerHTML swap (UI-SPEC §Polling LOCKED — never halts).

    Phase 88 (88-01, DRILL-03 / D-02): the ``#agents-table-section`` self-poll carries the pushed
    ``?agent=`` via ``hx-vals`` (agents_table.html), so this tick re-emits the selected-row highlight
    (aria-current + ring) on the matching row. Resolved by lookup-in-known-set (T-88-01) — an
    unknown/absent id highlights nothing, never a 422/500 into the poll.

    phaze-a6hm.4: the SAME mechanism now carries ``sort``/``order``, and it is what makes column
    sorting on this table possible at all. This section re-swaps ITSELF every 5s with
    ``hx-swap="outerHTML"`` and is spec'd never to halt, so a tick that omitted the operator's chosen
    sort would re-render under the default order and silently undo their click — with a 5-second fuse
    that no manual test shorter than 5 seconds can see. The partial renders its own resolved sort back
    into ``hx-vals``, so each tick re-sends the order the previous tick rendered and the choice is
    carried by the poll rather than merely surviving it. ``test_poll_tick_preserves_operator_sort``
    replays the tick to hold that.

    ``sort``/``order`` degrade to the default here (contract rule 3) rather than 422-ing: a stale
    bookmark or an evicted history entry can carry an old key perfectly innocently, and answering 422
    would blank the operator's whole page on a poll to punish a bad display preference.
    """
    sort_state = AGENTS_SORT.resolve(sort=sort, order=order)
    agents, now = await _load_agents(session, sort_state)
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
            "sort": sort_state,
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
