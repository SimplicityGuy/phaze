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

Two panels, one rule about which rows belong where (phaze-2u8v.4): Section 1 is the HEARTBEATING
table, so it renders only identities a persistent process actually beats for. A ``kind='compute'``
row that has never checked in belongs to an ephemeral Job-based lane and is represented by Section 2
instead — see ``_load_agents`` for why that suppression is keyed on the row's KIND rather than on
registry membership.

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
from sqlalchemy import BigInteger, DateTime, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 — FastAPI needs runtime import to resolve Annotated[AsyncSession, Depends(...)]

from phaze.config import get_settings
from phaze.database import get_session
from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.routers.column_sort import DESCENDING, SortableColumn, SortContract, SortState
from phaze.routers.response_shape import wants_fragment
from phaze.services.agent_liveness import ComputeLane, classify, derive_compute_lane_identities, get_compute_lane_running_jobs
from phaze.services.pg_text import contains_pg_invalid_chars
from phaze.services.pipeline import _agent_stage_buckets, get_agent_lane_depths, get_agent_recent_scans
from phaze.utils.humanize import relative_time
from phaze.web.static import static_asset_url


# The five pipeline stages surfaced in the agent-activity COUNT matrix (DRILL-02 / D-04). TRACKLIST is
# OMITTED (the 6-stage -> 5-pill remap, RESEARCH Pitfall 3); REVIEW renders under "Appr" and APPLY under
# "Exec" in the template. Ordered for the matrix's left-to-right column order.
_ACTIVITY_STAGES: tuple[Stage, ...] = (Stage.METADATA, Stage.ANALYZE, Stage.PROPOSE, Stage.REVIEW, Stage.APPLY)


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
#
# phaze-5yyh: cast to BigInteger (int8), NOT Integer (int4). ``queue_depth`` is committed as an int8
# domain on both the write side (schemas/agent_heartbeat.py's ``QUEUE_DEPTH_MAX = 10**12``) and the
# storage side (``_LANE_MERGE_SQL``'s ``SUM(... ::bigint)``), so a heartbeat above INT32_MAX (a valid
# value on that domain) is a legitimate JSONB value this ORDER BY must be able to sort, not an error.
# An int4 cast made Postgres raise DataError ("value ... out of range for type integer") on the whole
# statement -- 500ing this table (and, once queue-sorted, its own never-halting 5s self-poll) for
# every operator until the offending row was deleted.
_QUEUE_DEPTH_ORDER = func.coalesce(cast(Agent.last_status["queue_depth"].astext, BigInteger), -1)

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
exactly where a stale value does damage: the 5s self-poll re-requests THIS handler with no per-row
knowledge of what the operator last clicked, so a baked ``?agent=`` in ``poll_url()`` would re-assert a
FROZEN-AT-IMPORT-TIME selection every 5 seconds and erase the ring the operator just clicked (Phase 88
D-02). It travels in the section's ``hx-vals`` instead, read live from ``location.search`` at request
time, which htmx inherits to the sort buttons so a header click preserves the open row too (phaze-2u8v.6:
a row click itself overrides this same key with its OWN static ``hx-vals``, naming the row it belongs to
rather than reading a live value — see ``admin/partials/agents_table.html`` for that override and the
same disjointness note at the markup). The two channels are kept disjoint because htmx APPENDS
``hx-vals`` onto the ``hx-get`` query string — a key in both would be transmitted twice.
"""

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Expose the relative-time helper to all templates rendered through this
# router. The agents_table partial uses it via {{ humanize_relative_time(...) }}.
templates.env.globals["humanize_relative_time"] = relative_time
# phaze-315t: fingerprinted, cache-forever static asset URLs. `admin/agents.html` extends
# `base.html`, which carries the app.css link + favicon set.
templates.env.globals["static_url"] = static_asset_url

router = APIRouter(prefix="/admin/agents", tags=["admin"])


def _resolve_selected_agent(agent: str | None, agents: list[Agent]) -> str | None:
    """Resolve the pushed ``?agent=`` id by lookup-in-known-set (DRILL-03 / D-02, T-88-01).

    Returns the id only when it names a currently-loaded agent, so an unknown/absent/hostile id
    highlights nothing and can never reach a template as a trusted value. Kept ASCII/autoescaped.
    """
    return agent if agent is not None and any(a.id == agent for a in agents) else None


def _resolve_selected_compute_lane(clane: str | None, compute_lanes: list[ComputeLane]) -> str | None:
    """Resolve the pushed ``?clane=`` id by lookup-in-known-set (mirrors ``_resolve_selected_agent``, phaze-2u8v.5).

    Returns the id only when it names a currently-derived compute lane, so an unknown/absent/hostile
    id highlights nothing and can never reach a template as a trusted value.
    """
    return clane if clane is not None and any(lane.backend_id == clane for lane in compute_lanes) else None


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

    COMPUTE-01 (dedupe): a bearer-token ``kind='compute'`` Agent row exists ONLY to authenticate the
    one-shot ``job_runner`` callbacks of an ephemeral compute lane. Nothing behind it is a persistent
    process — a Kubernetes Job pod runs, calls back and exits; it never reaches the heartbeat endpoint.
    So every heartbeat column this table renders for such a row (STATUS / QUEUE / LAST SEEN / SCAN
    ROOTS) is ``NEVER`` / ``—`` / ``never`` / ``0`` by construction and forever, while the same lane
    reports ACTIVE with running workloads in Section 2. That contradiction is the "shown twice" defect,
    and it is what the Section-2 note means by "never as a perpetually-dead agent".

    We therefore suppress exactly that shape — ``kind=='compute'`` AND ``_status=='never'`` — and
    Section 2 is the sole representation of compute identities. The gate stays on ``_status=='never'``:
    a compute agent that has EVER heartbeated is a real process and keeps its row in every state,
    including a genuine ``dead`` after it stops, so a compute node going down is still visible here.
    Fileserver rows are untouched. Display-only (mirrors the revoked-row filter): no DB mutation, no
    schema change.

    phaze-2u8v.4: the predicate used to key on REGISTRY MEMBERSHIP — the row was dropped only when its
    id/name matched a non-local backend id (phaze-zlv) or a backend's bound ``agent_ref`` (phaze-ifcr).
    That made a display invariant depend on operator configuration, and both keys missed in the deployed
    registry: the kueue ``agent_ref`` binding was OPTIONAL and left unset there, and a lane that has been
    disabled (its ``[[backends]]`` block commented out) leaves its callback Agent row behind with NO
    registry key that can ever match. Both k8s rows leaked into Section 1 as perpetually-dead — the
    filter that was supposed to catch them had, in production, never once fired. Whether a row can
    heartbeat is a property of its KIND, not of what the operator happened to name it or of whether its
    cluster is currently enabled — so the suppression is now structural and reads no config at all.
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
    rows = [a for a in rows if not (a.kind == "compute" and a._status == "never")]  # type: ignore[attr-defined]
    # No Python re-sort: the order arrived from SQL above. Filtering never reorders a list, so the
    # shadow-row removal preserves the ORDER BY rather than needing it re-applied.
    return rows, now


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent: Annotated[str | None, Query()] = None,
    clane: Annotated[str | None, Query()] = None,
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
            # phaze-2u8v.5: same lookup-in-known-set idiom for the ?clane= compute-lane selection, so a
            # reload re-opens the dedicated #compute-lane-pane on the previously-selected burst lane.
            "selected_compute_lane": _resolve_selected_compute_lane(clane, compute_lanes),
            "sort": sort_state,
            "enable_saq_ui": get_settings().enable_saq_ui,  # CLEAN-01: gate the discreet /saq footer link (presentation-only)
        },
    )


@router.get("/_table", response_class=HTMLResponse)
async def table_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent: Annotated[str | None, Query()] = None,
    clane: Annotated[str | None, Query()] = None,
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
            "selected_compute_lane": _resolve_selected_compute_lane(clane, compute_lanes),
            "sort": sort_state,
        },
    )


@router.get("/{agent_id}/_activity", response_class=HTMLResponse)
async def agent_activity(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    agent_id: str,
) -> HTMLResponse:
    """Render the agent-activity body fragment swapped into its expanded-row slot (DRILL-02).

    phaze-2u8v.6: the wave-2 body for the agents-table EXPANDED ROW
    (``admin/partials/_agent_detail_row.html``), which replaced the shared non-modal ``#detail-pane``
    side panel for this surface. The agent-row trigger opens the row by re-fetching the whole table
    (``agents_table.html``); the row's own ``hx-get="/admin/agents/{id}/_activity"`` innerHTML-swaps THIS
    fragment into its per-agent slot ``#agent-activity-{id}``, and the fragment carries its OWN bounded
    ``hx-trigger="every 5s"`` self-refresh (D-03) — so the endpoint returns the body directly, NOT the
    row's chrome (the row is a static host with no body slot of its own).

    For a found agent the fragment stacks (D-05): liveness header (``classify`` transient ``_status`` +
    kind badge + last-seen) → a per-agent 6-stage COUNT matrix (one indexed ``GROUP BY stage_status_case``
    per stage scoped to ``agent_id`` via :func:`_agent_stage_buckets`, D-04/D-00a) → per-lane queue depths
    → recent scan batches. Every read is bounded + degrade-safe (D-00b): an unknown ``agent_id`` renders
    a friendly empty fragment at **200** (mirroring the sibling ``lane_detail`` never-error posture, T-88-07)
    — NEVER an ``HTTPException`` / JSON / 500 — and an agent owning 0 files renders the "owns no files yet"
    empty state. The 200 (not 404) is load-bearing: the ``/admin/agents`` page has no htmx 404 swap opt-in
    (its ``htmx:responseError`` handler targets ``#agents-table-section``, not this row's slot), so a 404
    fragment would be DISCARDED — the row would keep stale content and the self-poll would 404-loop forever
    on an agent revoked mid-view (WR-01/WR-02). The not-found fragment carries NO own-tick, so returning it
    at 200 terminates the poll loop cleanly.

    Read-only: ``get_session`` never commits and this handler issues no writes (T-88-10 — it reads ONLY
    the derived ``stage_status_case``, never ``FileRecord.state``).
    """
    now = datetime.now(UTC)
    # phaze-oldp: PostgreSQL cannot bind a NUL (U+0000) or lone surrogate in a UTF8 text
    # comparison at all -- session.get() would raise asyncpg CharacterNotInRepertoireError
    # before it could ever match a row, which is indistinguishable in outcome from "no such
    # agent". Route it through the same friendly-empty-fragment branch this docstring already
    # promises for "unknown/hostile id" rather than letting the DB reject the bind.
    agent = None if contains_pg_invalid_chars(agent_id) else await session.get(Agent, agent_id)
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


@router.get("/compute-lanes/{backend_id}", response_class=HTMLResponse)
async def compute_lane_detail(
    request: Request,
    backend_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """Return the burst-lane workload drill-down fragment, innerHTML-swapped into the dedicated ``#compute-lane-pane`` (phaze-2u8v.5).

    The burst-lane panel's "N workloads · N running" tally had no way to see WHAT was running — an
    operator could see that work existed but not act on it. This mirrors the sibling drill-down
    endpoints (``lane_detail`` / ``agent_activity``): the compute-lane card's ``hx-get`` swaps THIS
    fragment into ``admin/partials/_compute_lane_pane.html``'s ``#compute-lane-pane`` (a DEDICATED
    fixed/slide-in shell, not the Analyze workspace's shared 88-01 ``_detail_pane.html`` — see that
    partial's docstring for why), and the fragment carries its own bounded ``hx-trigger="every 5s"``
    self-refresh (D-03).

    ``backend_id`` is resolved by lookup-in-known-set against :func:`derive_compute_lane_identities`
    (T-88-03 idiom): an unknown/stale id (a backend removed from the registry mid-view) renders the
    friendly "Compute lane offline" empty fragment at 200 — never a 404/500 — so a poll never breaks
    on a since-removed backend. A resolved lane lists its RUNNING workloads via
    :func:`get_compute_lane_running_jobs`, each identified by the file it is processing rather than
    just a count; an IDLE lane (0 running) renders the plain "No workloads currently running" empty
    state, satisfying the idle-as-well-as-active acceptance.

    Read-only — no commit.
    """
    lanes = await derive_compute_lane_identities(session)
    lane = next((one for one in lanes if one.backend_id == backend_id), None)
    running_jobs = await get_compute_lane_running_jobs(session, backend_id) if lane is not None else []
    return templates.TemplateResponse(
        request=request,
        name="admin/partials/_compute_lane_detail.html",
        context={
            "lane": lane,
            "backend_id": backend_id,
            "running_jobs": running_jobs,
            "refreshed_at": datetime.now(UTC),
        },
    )
