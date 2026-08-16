"""GET /admin/agents (redirect) + GET /admin/agents/_table (HTMX partial) — Phase 29 D-11..D-14.

Operator-facing read-only admin page that lists every non-revoked registered
file-server agent with a status pill (alive/stale/dead/never). Revoked agents
are filtered out entirely (see ``_load_agents``). The page polls
``/admin/agents/_table`` every 5 seconds via an HTMX self-replacing
``<section>`` (UI-SPEC §Polling LOCKED — never halts).

phaze-uvmcr.4: the full-page render itself moved into the shell (``routers/shell.py``
``UTILITY_PANES["agents"]``, reachable at ``GET /s/agents`` with the rail intact). ``GET
/admin/agents`` now 301-redirects there, preserving its query string; see :func:`page` and
:func:`build_agents_pane_context`, the context-builder now SHARED by the shell's ``agents`` stage.
Every fragment endpoint below this module's prefix (``/_table``, ``/{agent_id}/_activity``,
``/compute-lanes/{backend_id}``) is UNCHANGED, in path and behavior.

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

One merged table, one rule about which rows belong where (phaze-rdxfu, superseding phaze-2u8v.4's
two-panel split): every burst lane (``services.agent_liveness.derive_compute_lane_identities``) now
renders as a ROW in the SAME ``#agents-table-section`` table as heartbeating Agent rows, normalized
through :class:`TableRow` (see "UNIFIED ROW MODEL" on that class). A ``kind='compute'`` Agent row that
has never checked in is still suppressed from its OWN row — it is the exact bearer-token callback
identity a lane row already represents, and rendering both would reintroduce the "shown twice" defect
phaze-2u8v.4 fixed — see ``_load_agents`` for why that suppression is keyed on the row's KIND rather
than on registry membership. Lane rows can never carry an agent 5-state status (KDEPLOY-04): the
merge is structural, not a rendering convention, so there is no code path from a lane to a DEAD pill.

Auth posture: NO ``get_authenticated_agent`` dependency — operator pages are
open on the private LAN (consistent with pipeline.py / pipeline_scans.py
precedent; CONTEXT.md D-discretion).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import BigInteger, DateTime, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002 — FastAPI needs runtime import to resolve Annotated[AsyncSession, Depends(...)]

from phaze.config import get_settings
from phaze.database import get_session
from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.routers.column_sort import DESCENDING, SortableColumn, SortContract, SortState
from phaze.services.agent_liveness import ComputeLane, classify, derive_compute_lane_identities, get_compute_lane_running_jobs
from phaze.services.pg_text import contains_pg_invalid_chars
from phaze.services.pipeline import _agent_stage_buckets, get_agent_lane_depths, get_agent_recent_scans
from phaze.utils.humanize import relative_time


if TYPE_CHECKING:
    from collections.abc import Sequence


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


def _unresolved_detail_carrier_ids(
    agent: str | None,
    clane: str | None,
    *,
    selected_agent: str | None,
    selected_compute_lane: str | None,
) -> list[str]:
    """DOM ids of ``hx-preserve`` carrier rows for selections that arrived but failed lookup (phaze-w92dg).

    The expanded detail row only survives a 5s table poll if the poll's response contains a node with
    its id (``hx-preserve`` matches by id against incoming content). Lookup-in-known-set therefore had
    a destructive side effect: one tick on which the selection failed to resolve — a degraded
    ``derive_compute_lane_identities`` read returning ``[]``, an agent leaving the non-revoked set —
    omitted the detail row from the response and htmx tore down the operator's live open row. The
    operator rule is that the detail NEVER auto-collapses: only Esc/✕ (which drop the param from the
    URL, so no carrier is emitted afterwards) may dismiss it.

    For each selection param that is present-but-unresolved, return the detail-row DOM id (the same
    format :attr:`TableRow.detail_row_id` produces) so ``agents_table.html`` can emit a minimal empty
    carrier ``<tr>`` under that id. When an open row exists, hx-preserve keeps it in the carrier's
    place; when none does (a bogus deep-link id), the carrier renders as an empty invisible row — the
    hostile param reaches the template only as an autoescaped ``id`` attribute, never a query.
    """
    carriers: list[str] = []
    if agent and selected_agent is None:
        carriers.append(f"agent-detail-row-{agent}")
    if clane and selected_compute_lane is None:
        carriers.append(f"compute-lane-detail-row-{clane}")
    return carriers


@dataclass(frozen=True, slots=True)
class TableRow:
    """One row of the merged ``/admin/agents`` table — an Agent OR a derived ComputeLane, normalized (phaze-rdxfu).

    UNIFIED ROW MODEL: :class:`~phaze.models.agent.Agent` (a SQLAlchemy ORM row) and
    :class:`~phaze.services.agent_liveness.ComputeLane` (a frozen dataclass) share no supertype, and
    ``agents_table.html`` used to reach into ``agent.last_status`` / ``agent.scan_roots`` /
    ``agent._status`` directly — which would have meant teaching the template to branch on
    ``isinstance`` in every cell. This view-model normalizes both shapes into ONE at the router
    boundary instead, so the template renders a single loop over ``TableRow`` and never sees an Agent
    or a ComputeLane directly. ``_kind_badge.html`` / ``_status_pill.html`` stay the two places that
    own palette, gated on :attr:`status_kind` rather than ``isinstance``, which is what makes the
    never-DEAD invariant (KDEPLOY-04) a STRUCTURAL property: no ``status_kind == "lane"`` row can ever
    carry one of the 5 agent status strings, because a lane row's :attr:`status` is only ever set from
    :data:`~phaze.services.agent_liveness.ComputeLaneState`.

    Attributes:
        name: The primary label — ``agent.name`` or ``lane.backend_id``.
        id: The row's identity — ``agent.id`` or ``lane.backend_id``.
        kind: ``agent.kind`` ("compute"/"fileserver") or ``lane.kind`` ("compute"/"kueue"/...). The
            SAME string space overlaps ("compute" means different things for an agent vs. a lane), so
            ``_kind_badge.html`` always branches on :attr:`status_kind` FIRST, never on this alone.
        status: ``agent._status`` (the 5-state alive/stale/dead/revoked/never) or ``lane.state`` (the
            3-state ACTIVE/WAITING/IDLE) — which one is decided by :attr:`status_kind`.
        status_kind: Discriminates the two row shapes. The ONLY field a template needs to branch on.
        queue: Queue depth — an agent's reported ``queue_depth`` (``None`` when absent, rendered as an
            em-dash) or a lane's ``waiting`` (quota-pending, always present).
        last_seen: ``agent.last_seen_at``, or ``None`` for a lane (no heartbeat concept at all — always
            renders as an em-dash, never the "never" a ``None`` agent value would render).
        scan_roots: ``len(agent.scan_roots)``, or ``None`` for a lane (renders as an em-dash).
        detail_url: The ``hx-get`` target for the expanded row's body slot.
        selection_param: The query-string param this row's selection travels under — ``"agent"`` or
            ``"clane"`` — so the two channels stay disjoint exactly as the pre-merge two-panel page
            kept them (``AGENTS_SORT``'s module docstring on why ``?agent=``/``?clane=`` never ride in
            ``view_state``).
        secondary_id: The muted id line under the name (agent rows only — a lane's AGENT cell is just
            ``backend_id``, no secondary line, per acceptance).
        selected: Whether this row is the one currently expanded (server-resolved lookup-in-known-set,
            same T-88-01/phaze-2u8v.5 idiom the pre-merge page used).
    """

    name: str
    id: str
    kind: str
    status: str
    status_kind: Literal["agent", "lane"]
    queue: int | None
    last_seen: datetime | None
    scan_roots: int | None
    detail_url: str
    selection_param: Literal["agent", "clane"]
    secondary_id: str | None = None
    selected: bool = False

    @property
    def trigger_id(self) -> str:
        """The stable DOM id of this row's ``<tr>`` — unchanged for an agent row (phaze-2u8v.6 ids)."""
        prefix = "agent-trigger-" if self.status_kind == "agent" else "compute-lane-trigger-"
        return f"{prefix}{self.id}"

    @property
    def detail_row_id(self) -> str:
        """The DOM id of this row's expanded ``<tr>`` — unchanged for an agent row (phaze-2u8v.6 ids)."""
        prefix = "agent-detail-row-" if self.status_kind == "agent" else "compute-lane-detail-row-"
        return f"{prefix}{self.id}"

    @property
    def push_url(self) -> str:
        """The base ``hx-push-url`` this row's click pushes (before ``sort.query_state()`` is appended)."""
        return f"/s/agents?{self.selection_param}={self.id}"


def _agent_sort_value(agent: Agent, key: str) -> Any:
    """Return the Python-comparable value for ``agent`` under sort ``key`` (mirrors the SQL fold).

    Used ONLY to decide where a lane falls RELATIVE to the already SQL-ordered agent list (see
    :func:`_merge_lanes_into_agents`) — never to re-sort ``agents`` itself, which stays exactly the
    order SQL returned. Mirrors :data:`_LAST_SEEN_ORDER` / :data:`_QUEUE_DEPTH_ORDER` so a lane's
    computed placement lands where the SAME fold would put it if it could join the SQL ORDER BY.
    """
    if key == "name":
        return agent.name
    if key == "kind":
        return agent.kind
    if key == "queue":
        depth = (agent.last_status or {}).get("queue_depth")
        return depth if isinstance(depth, int) else -1
    if key == "last_seen":
        return agent.last_seen_at.timestamp() if agent.last_seen_at is not None else -math.inf
    if key == "scan_roots":
        return len(agent.scan_roots or [])
    raise AssertionError(f"unreachable: {key!r} is not a whitelisted AGENTS_SORT column")  # pragma: no cover


def _lane_sort_value(lane: ComputeLane, key: str) -> Any:
    """Return the Python-comparable value for ``lane`` under sort ``key`` (phaze-rdxfu SCOPE rule 3).

    ``name``/``kind``/``queue`` reuse the SAME column semantics an agent row would (real values,
    directly comparable — a lane's ``waiting`` genuinely competes with an agent's queue depth).
    ``last_seen``/``scan_roots`` are concepts a lane has none of, so they fold to the "no value" end
    EXACTLY the way :data:`_LAST_SEEN_ORDER` / :data:`_QUEUE_DEPTH_ORDER` already fold a NULL agent
    value — a lane sorts precisely where a never-seen agent (or a queue-depth-less agent) would.
    """
    if key == "name":
        return lane.backend_id
    if key == "kind":
        return lane.kind
    if key == "queue":
        return lane.waiting
    if key == "last_seen":
        return -math.inf
    if key == "scan_roots":
        return -1
    raise AssertionError(f"unreachable: {key!r} is not a whitelisted AGENTS_SORT column")  # pragma: no cover


def _merge_lanes_into_agents(agents: Sequence[Agent], lanes: Sequence[ComputeLane], sort: SortState) -> list[Agent | ComputeLane]:
    """Interleave derived compute lanes into the already SQL-ordered agent list (phaze-rdxfu SCOPE rule 3).

    Agent-relative order is untouched by this function — it never re-sorts ``agents``, only decides
    where each lane falls RELATIVE to them, so SQL stays the sole source of agent ordering (a Python
    re-sort over the merged list is exactly the drift :data:`AGENTS_SORT`'s docstring warns a future
    caller away from).

    Lanes are first sorted among THEMSELVES by the same column (a stable sort, so lanes tied on the
    column keep their :func:`~phaze.services.agent_liveness.derive_compute_lane_identities` registry
    order), then merged in via a two-pointer pass: at each step a lane is placed before the next agent
    only when it sorts STRICTLY before it under the resolved direction, so an exact tie always resolves
    agent-first. That tie rule is a pure function of the two values being compared — never of
    iteration/hash order — so it is deterministic and stable across polls: a lane can never reshuffle
    relative to a stationary agent between two 5s ticks that read the identical underlying data.
    """
    key = sort.key
    order = sort.order
    ordered_lanes = sorted(lanes, key=lambda lane: _lane_sort_value(lane, key), reverse=(order == DESCENDING))

    def _sorts_before(lane_value: Any, agent_value: Any) -> bool:
        return bool(lane_value > agent_value) if order == DESCENDING else bool(lane_value < agent_value)

    merged: list[Agent | ComputeLane] = []
    i, j = 0, 0
    while i < len(agents) and j < len(ordered_lanes):
        if _sorts_before(_lane_sort_value(ordered_lanes[j], key), _agent_sort_value(agents[i], key)):
            merged.append(ordered_lanes[j])
            j += 1
        else:
            merged.append(agents[i])
            i += 1
    merged.extend(agents[i:])
    merged.extend(ordered_lanes[j:])
    return merged


def _agent_table_row(agent: Agent, *, selected_agent: str | None) -> TableRow:
    """Normalize one non-revoked, classified Agent into a :class:`TableRow` (phaze-rdxfu)."""
    queue_depth = (agent.last_status or {}).get("queue_depth") if agent.last_status else None
    return TableRow(
        name=agent.name,
        id=agent.id,
        kind=agent.kind,
        status=agent._status,  # type: ignore[attr-defined]  # Phase 27 transient-attr pattern
        status_kind="agent",
        queue=queue_depth if isinstance(queue_depth, int) else None,
        last_seen=agent.last_seen_at,
        scan_roots=len(agent.scan_roots or []),
        detail_url=f"/admin/agents/{agent.id}/_activity",
        selection_param="agent",
        secondary_id=agent.id,
        selected=selected_agent == agent.id,
    )


def _lane_table_row(lane: ComputeLane, *, selected_compute_lane: str | None) -> TableRow:
    """Normalize one derived :class:`ComputeLane` into a :class:`TableRow` (phaze-rdxfu).

    ``last_seen``/``scan_roots`` are ``None`` (never a real value, never "never") — the template
    renders the same em-dash it already uses for absent data (acceptance rule 2), and ``status`` is
    only ever one of :data:`~phaze.services.agent_liveness.ComputeLaneState`'s 3 members, so DEAD stays
    structurally impossible for a lane row (KDEPLOY-04).
    """
    return TableRow(
        name=lane.backend_id,
        id=lane.backend_id,
        kind=lane.kind,
        status=lane.state,
        status_kind="lane",
        queue=lane.waiting,
        last_seen=None,
        scan_roots=None,
        detail_url=f"/admin/agents/compute-lanes/{lane.backend_id}",
        selection_param="clane",
        secondary_id=None,
        selected=selected_compute_lane == lane.backend_id,
    )


def _build_table_rows(
    agents: Sequence[Agent],
    compute_lanes: Sequence[ComputeLane],
    sort: SortState,
    *,
    selected_agent: str | None,
    selected_compute_lane: str | None,
) -> list[TableRow]:
    """Merge Agent + ComputeLane rows into ONE ordered, normalized view-model list (phaze-rdxfu).

    The single entry point ``page``/``table_partial`` call after loading both sources: merges lane
    placement into the SQL-ordered agent list (:func:`_merge_lanes_into_agents`) and normalizes every
    item into a :class:`TableRow` (:func:`_agent_table_row` / :func:`_lane_table_row`) so
    ``agents_table.html`` renders one loop with no isinstance branching.
    """
    merged = _merge_lanes_into_agents(agents, compute_lanes, sort)
    return [
        _agent_table_row(item, selected_agent=selected_agent)
        if isinstance(item, Agent)
        else _lane_table_row(item, selected_compute_lane=selected_compute_lane)
        for item in merged
    ]


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
    ROOTS) is ``NEVER`` / ``—`` / ``never`` / ``0`` by construction and forever, while the SAME lane's
    OWN row (phaze-rdxfu: now a row in this same table, not a separate section) reports ACTIVE with
    running workloads. That contradiction is the "shown twice" defect this suppression exists to
    prevent — a lane must never simultaneously read as ACTIVE and as a perpetually-dead agent.

    We therefore suppress exactly that shape — ``kind=='compute'`` AND ``_status=='never'`` — so the
    lane's own row (built separately by :func:`_lane_table_row` from
    :func:`~phaze.services.agent_liveness.derive_compute_lane_identities`) is the SOLE representation
    of that compute identity. The gate stays on ``_status=='never'``: a compute agent that has EVER
    heartbeated is a real process and keeps its row in every state, including a genuine ``dead`` after
    it stops, so a compute node going down is still visible here. Fileserver rows are untouched.
    Display-only (mirrors the revoked-row filter): no DB mutation, no schema change.

    phaze-2u8v.4: the predicate used to key on REGISTRY MEMBERSHIP — the row was dropped only when its
    id/name matched a non-local backend id (phaze-zlv) or a backend's bound ``agent_ref`` (phaze-ifcr).
    That made a display invariant depend on operator configuration, and both keys missed in the deployed
    registry: the kueue ``agent_ref`` binding was OPTIONAL and left unset there, and a lane that has been
    disabled (its ``[[backends]]`` block commented out) leaves its callback Agent row behind with NO
    registry key that can ever match. Both k8s rows leaked into the heartbeating table as
    perpetually-dead — the filter that was supposed to catch them had, in production, never once fired.
    Whether a row can heartbeat is a property of its KIND, not of what the operator happened to name it
    or of whether its cluster is currently enabled — so the suppression is now structural and reads no
    config at all.
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


async def build_agents_pane_context(request: Request, session: AsyncSession) -> dict[str, Any]:
    """Build every context key ``admin/agents.html`` needs, from ``request.query_params`` alone (phaze-uvmcr.4).

    Extracted out of the former ``page()`` full-page branch so ``routers/shell.py``'s ``agents``
    stage can build the IDENTICAL context ``page()`` used to -- one assembly, two callers, so the
    shell-hosted pane and the (now-redirecting) legacy route can never quietly diverge on what a
    render of this content actually needs.

    ``agent``/``clane``/``sort``/``order`` are read straight off ``request.query_params`` rather
    than declared as typed ``Query()`` parameters -- mirroring ``shell._render_stage``'s existing
    ``analyze`` branch, which resolves its own ``?lane=`` the same way. This keeps the wire-bounds
    contract (``tests/shared/schemas/test_wire_bounds_contract.py``) scoped to the routes that
    actually declare these as FastAPI parameters (``/admin/agents/_table``, unchanged) rather than
    minting a second, redundant classification for ``GET /s/{stage}``.

    The returned mapping is merged into the caller's base shell context; it is never a whole
    context on its own (it carries no ``request``/``stage``/``oob_counts`` keys).
    """
    agent = request.query_params.get("agent")
    clane = request.query_params.get("clane")
    sort = request.query_params.get("sort")
    order = request.query_params.get("order")
    sort_state = AGENTS_SORT.resolve(sort=sort, order=order)
    agents, now = await _load_agents(session, sort_state)
    # phaze-rdxfu (was Section 2 / RECORD-03 / D-07 → COMPUTE-01): one ephemeral compute-lane identity
    # PER non-local registry backend, synthesized read-only from the Phase-67 registry + in-flight
    # CloudJob counts. Merged into the SAME row list as heartbeating agents below, so the existing 5s
    # self-poll refreshes lane rows too (no new loop, RESEARCH OQ-1).
    compute_lanes = await derive_compute_lane_identities(session)
    selected_agent = _resolve_selected_agent(agent, agents)
    # phaze-2u8v.5 lookup-in-known-set idiom, kept verbatim: unknown/absent id highlights nothing.
    selected_compute_lane = _resolve_selected_compute_lane(clane, compute_lanes)
    rows = _build_table_rows(agents, compute_lanes, sort_state, selected_agent=selected_agent, selected_compute_lane=selected_compute_lane)
    return {
        "rows": rows,
        "agent_count": len(agents),
        "compute_lane_count": len(compute_lanes),
        "now": now,
        "refreshed_at_iso": now.isoformat(),
        "sort": sort_state,
        "unresolved_detail_carrier_ids": _unresolved_detail_carrier_ids(
            agent, clane, selected_agent=selected_agent, selected_compute_lane=selected_compute_lane
        ),
        "enable_saq_ui": get_settings().enable_saq_ui,  # CLEAN-01: gate the discreet /saq footer link (presentation-only)
    }


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> Response:
    """GET /admin/agents -- redirect (301) to /s/agents, preserving the query string (phaze-uvmcr.4).

    ``"agents"`` is now a registered shell utility pane (``routers/shell.py`` ``UTILITY_PANES``),
    reachable with the rail intact at ``/s/agents``, which renders the SAME content this handler used
    to (``admin/agents.html``, converted to a content-only partial -- see
    :func:`build_agents_pane_context`, now shared by both). Rendering a second, rail-less copy of
    that content here would be exactly the redundant fork ``phaze-uvmcr.2`` retired for
    ``/pipeline/files`` -- this route is the last remaining full-page one.

    The redirect is UNCONDITIONAL, not shape-forked through ``wants_fragment`` the way the
    ``/pipeline/files`` precedent was: no in-tree caller ever issues a live htmx swap against this
    bare path (the 5s poll and every sort/drill-in click target ``/admin/agents/_table`` directly),
    so there is no fragment-wanting shape left to preserve here. A history-restore request also
    redirects -- following the redirect a second time lands it on the real full document
    (``shell.shell_stage``), which is what a restore needs regardless of which URL it started from.

    301 (permanent), per acceptance: this path is being retired in favor of ``/s/agents``, not
    merely temporarily rerouted while both stay canonical.

    The query string carries ``?agent=``/``?clane=``/``?sort=``/``?order=`` verbatim -- phaze-rdxfu's
    ``?clane=`` deep-link acceptance depends on the round trip, so this passes the RAW string through
    rather than re-serializing individually-typed params that could drop or reorder one.

    SCOPE (do not "tidy" this later): the fragment endpoints below --
    ``GET /admin/agents/_table`` (the 5s poll target), ``GET /admin/agents/{agent_id}/_activity``,
    ``GET /admin/agents/compute-lanes/{backend_id}`` -- are UNCHANGED, in path and behavior. They are
    swap targets referenced by many templates and tests, not bookmarkable pages, and moving them
    would multiply this bead's diff for no operator-visible gain.
    """
    query = request.url.query
    return RedirectResponse(url=f"/s/agents?{query}" if query else "/s/agents", status_code=301)


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
    selected_agent = _resolve_selected_agent(agent, agents)
    selected_compute_lane = _resolve_selected_compute_lane(clane, compute_lanes)
    rows = _build_table_rows(agents, compute_lanes, sort_state, selected_agent=selected_agent, selected_compute_lane=selected_compute_lane)
    return templates.TemplateResponse(
        request=request,
        name="admin/partials/agents_table.html",
        context={
            "request": request,
            "rows": rows,
            "agent_count": len(agents),
            "compute_lane_count": len(compute_lanes),
            "now": now,
            "refreshed_at_iso": now.isoformat(),
            "sort": sort_state,
            "unresolved_detail_carrier_ids": _unresolved_detail_carrier_ids(
                agent, clane, selected_agent=selected_agent, selected_compute_lane=selected_compute_lane
            ),
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
    """Return the burst-lane workload drill-down fragment, innerHTML-swapped into its EXPANDED ROW's body slot (phaze-rdxfu).

    The burst-lane panel's "N workloads · N running" tally had no way to see WHAT was running — an
    operator could see that work existed but not act on it. This mirrors the sibling drill-down
    endpoint (``agent_activity``): a lane row's ``hx-get`` swaps THIS fragment into
    ``admin/partials/_compute_lane_detail_row.html``'s per-lane ``#compute-lane-activity-{backend_id}``
    slot — the SAME expanded-row shape ``_agent_detail_row.html`` uses for an agent, not the Analyze
    workspace's shared 88-01 ``_detail_pane.html`` (see that partial's docstring for why) and no longer
    the dedicated side pane phaze-2u8v.5 introduced (``admin/partials/_compute_lane_pane.html`` — this
    bead deletes it; every drill-in on ``/admin/agents`` is now an expanded row). The fragment carries
    its own bounded ``hx-trigger="every 5s"`` self-refresh (D-03).

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
