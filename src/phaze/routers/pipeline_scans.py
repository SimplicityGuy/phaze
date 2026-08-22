"""GET /pipeline/scans/{batch_id} (HTMX poll) + /recent (table) + /agent-roots (agent-dropdown swap) + DELETE -- Phase 27 D-05..D-08.

This is the operator-facing admin router (Wave 3) that closes SCAN-01: the read, render
and delete half of the `/pipeline/scans` prefix. The POST trigger that creates a scan
lives in `routers/scan.py` (split out by phaze-bk9el.17, a verbatim move); both routers
are registered on the production app, so every path under this prefix is unchanged.

The poll endpoint (`GET /pipeline/scans/{batch_id}`) returns the same template
keyed off `batch.status`; terminal-state markup OMITS `hx-trigger`/`hx-get` so the
HTMX `outerHTML` swap halts polling automatically (Pitfall 6).

The agent-roots swap (`GET /pipeline/scans/agent-roots`) re-renders the
`scan_path_picker.html` partial with the chosen agent's `scan_roots` jsonb entries
populated; missing/revoked/empty agents render the yellow-surface empty state.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.column_sort import DESCENDING, SortableColumn, SortContract, SortState
from phaze.routers.response_shape import RENDERABLE_ALERT_STATUS
from phaze.services.pipeline import get_agent_reconciliations
from phaze.services.scan_deletion import delete_scan_cascade


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/pipeline/scans", tags=["pipeline"])

# Terminal states freeze the elapsed timer; RUNNING/LIVE keep ticking. ScanStatus
# is a StrEnum, so `batch.status` (a plain str) compares/hashes by value against
# these members -- `batch.status in _TERMINAL_STATUSES` works directly.
_TERMINAL_STATUSES = frozenset({ScanStatus.COMPLETED, ScanStatus.FAILED})

# PR4: the UI flips to an amber "stalled?" warning at HALF the reaper's hard
# scan_stall_seconds threshold, so the operator sees a warning *before* the
# reaper actually marks the scan FAILED. e.g. scan_stall_seconds=600 -> the UI
# warns once a RUNNING scan has been quiet for >300s.
_UI_STALL_WARN_FRACTION = 0.5

# phaze-a6hm.6: THE sortable-column contract for the Recent Scans table (see
# `routers/column_sort.py` -- that docstring is the contract, this is only its wiring).
# Module-level per contract rule 6, so a mis-wired column fails at import, not per click.
#
# The AGENT column sorts by the agent's NAME, not its id: the cell displays `_agent_name`, and a
# header that visibly reads "Agent" but orders by an opaque id would be a sort the operator cannot
# verify by looking. A correlated scalar subquery is a legal `expression` (rule 2) and keeps
# `build_recent_scans`' single-query shape -- no join, so the LIMIT still applies to ScanBatch rows.
#
# ELAPSED and ACTIONS are deliberately NOT whitelisted. Elapsed is computed in PYTHON
# (`elapsed_seconds`, which branches on completed_at/updated_at) and has no column to ORDER BY;
# offering it would mean sorting `rows` after the read, which contract rule 1 forbids because it
# reorders only the fetched window. Their headers render as plain text -- the template gates on
# `sort.is_sortable(...)`, so an un-whitelisted label needs no per-column branching here.
RECENT_SCANS_SORT = SortContract(
    endpoint="/pipeline/scans/recent",
    target="#recent-scans",
    columns=(
        SortableColumn(key="agent", label="Agent", expression=select(Agent.name).where(Agent.id == ScanBatch.agent_id).scalar_subquery()),
        SortableColumn(key="path", label="Path", expression=ScanBatch.scan_path),
        SortableColumn(key="status", label="Status", expression=ScanBatch.status),
        SortableColumn(key="files", label="Files", expression=ScanBatch.processed_files),
        SortableColumn(key="started", label="Started", expression=ScanBatch.created_at),
    ),
    default_key="started",
    # The pre-sort behaviour was `ORDER BY created_at DESC LIMIT 10` and operators read this table
    # newest-first; defaulting to ASC would silently re-point "Recent Scans" at the OLDEST scans.
    default_order=DESCENDING,
)


def elapsed_seconds(batch: ScanBatch) -> int:
    """Compute integer seconds elapsed since `batch.created_at`.

    The actual postgres column type is `TIMESTAMP WITH TIME ZONE` (asyncpg
    materializes that as a tz-aware `datetime` with `tzinfo=UTC`), so we
    compare aware-to-aware. A previous implementation stripped tzinfo from
    `now` to match an assumed-naive `created_at` and crashed with
    `TypeError: can't subtract offset-naive and offset-aware datetimes`.
    `created_at` is NOT NULL at the ORM layer (Mapped[datetime] without
    `| None`), so no None branch is needed.

    If `created_at` is unexpectedly tz-naive (e.g., a model loaded from a
    test fixture that bypassed the DB type coercion), assume UTC so the
    subtraction still produces a meaningful elapsed value.

    Incident 260608/260609: the elapsed value freezes for terminal batches in
    two cases, in this precedence:

      1. `completed_at` is set -> freeze at `completed_at - created_at`.
      2. else if the batch is terminal (COMPLETED/FAILED) but `completed_at`
         is NULL (legacy / pre-backfill rows -- incident 260609) -> freeze at
         `updated_at - created_at`, the recorded transition time. If
         `updated_at` is somehow also NULL, fall back to `now` so this never
         crashes.

    A RUNNING (non-terminal) batch keeps tracking `now - created_at`. The same
    tz-naive->UTC safety is applied to `completed_at` and `updated_at`.

    Phase 27 UAT gap-14: shared helper -- previously a private
    `_elapsed_seconds` here was duplicated inline in
    `phaze.routers.pipeline.dashboard`. The duplicate carried the
    pre-gap-12 antipattern (`datetime.now(UTC).replace(tzinfo=None) -
    batch.created_at`) and crashed the dashboard the first time the
    Recent Scans table loaded a real tz-aware row. Now both routers
    import this one definition.
    """
    created_at = batch.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)

    if batch.completed_at is not None:
        end = batch.completed_at
    elif batch.status in _TERMINAL_STATUSES:
        # Terminal row whose completed_at was never stamped: freeze at updated_at.
        end = batch.updated_at if batch.updated_at is not None else datetime.now(UTC)
    else:
        end = datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return int((end - created_at).total_seconds())


def seconds_since_progress(batch: ScanBatch) -> int:
    """Integer seconds since the scan last made progress (PR4 activity indicator).

    Uses ``last_progress_at`` (the per-progress heartbeat), falling back to
    ``created_at`` for legacy rows that predate the heartbeat column. Mirrors
    ``elapsed_seconds``' tz-aware-safe handling: a tz-naive timestamp (e.g. from
    a test fixture whose schema is TIMESTAMP WITHOUT TIME ZONE) is assumed UTC so
    the subtraction stays aware-to-aware and never crashes.
    """
    ref = batch.last_progress_at or batch.created_at
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return int((now - ref).total_seconds())


def is_scan_stalled(batch: ScanBatch) -> bool:
    """True when a RUNNING batch has been quiet past the UI warn threshold (PR4).

    The warn threshold is half the reaper's ``scan_stall_seconds`` so the amber
    "stalled?" affordance surfaces before the reaper hard-fails the scan. Only
    RUNNING batches can be "stalled" in the UI sense; terminal/LIVE rows return
    False.
    """
    if batch.status != ScanStatus.RUNNING.value:
        return False
    warn_threshold = int(get_settings().scan_stall_seconds * _UI_STALL_WARN_FRACTION)
    return seconds_since_progress(batch) > warn_threshold


@router.get("/agent-roots", response_class=HTMLResponse)
async def agent_roots_swap(
    request: Request,
    agent_id: Annotated[str, Query(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """HTMX swap target: render `scan_path_picker.html` for the chosen agent.

    Empty/missing/revoked agents render the yellow-surface empty state
    (UI-SPEC §"Empty scan_roots case" lines 245-250).
    """
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.revoked_at is not None or not agent.scan_roots:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_path_picker.html",
            context={"request": request, "agent": None},
        )
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_path_picker.html",
        context={"request": request, "agent": agent},
    )


@router.get("/recent", response_class=HTMLResponse)
async def recent_scans_partial(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    sort: Annotated[str | None, Query()] = None,
    order: Annotated[str | None, Query()] = None,
    poll: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """HTMX poll endpoint: re-render the Recent Scans mini-table.

    Returns the same ``recent_scans_table.html`` partial the dashboard renders at
    page load (and that ``delete_scan`` re-renders after a delete), via the shared
    ``build_recent_scans`` helper. The partial's root ``<section id="recent-scans">``
    carries ``hx-get="/pipeline/scans/recent" hx-trigger="every 5s"
    hx-swap="outerHTML"``, so each swapped-in copy re-arms the poll -- the same
    self-referential pattern as ``scan_progress_card.html``.

    Registered BEFORE ``GET /pipeline/scans/{batch_id}`` so the literal ``/recent``
    path is matched here instead of being captured as a ``batch_id`` UUID path param.

    phaze-a6hm.6: this endpoint serves BOTH the header-click re-sort and the 5s poll, which is why
    it takes ``sort``/``order`` -- and why the partial it returns spells its own ``hx-get`` as
    ``sort.poll_url()`` (column_sort contract rule 4a). Because the swap is ``outerHTML``, the
    response REPLACES the polling element, so each tick's sort is whatever the previous response
    wrote into ``hx-get``. Rendering the default here while the operator has a sort chosen would
    therefore not merely skip one tick: the swapped-in copy would carry the default URL forward and
    the chosen sort would be gone for good, ~5s after the click.

    Both values are untrusted strings from the wire; ``resolve`` maps anything unrecognised to the
    contract default (rule 3) rather than 422-ing a poll that fires every five seconds.

    phaze-8f9j: ``poll=0`` marks a caller that must NOT get a self-polling copy back -- the Discover
    workspace, which mounts this table inside the v7 shell where WORK-05 allows exactly one poll.
    The flag is re-emitted through the sort contract's ``view_state`` so it survives every header
    click, for the same reason rule 4 makes the sort itself survive. Any other value (including
    absent) polls, so no existing caller changes behaviour.
    """
    polling = poll != "0"
    sort_state = RECENT_SCANS_SORT.resolve(sort=sort, order=order, view_state={} if polling else {"poll": "0"})
    rows = await build_recent_scans(session, sort=sort_state)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/recent_scans_table.html",
        context={"request": request, "recent_scans": rows, "sort": sort_state, "scans_poll": polling},
    )


@router.get("/{batch_id}", response_class=HTMLResponse)
async def scan_progress(
    request: Request,
    batch_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> HTMLResponse:
    """HTMX poll endpoint: return `scan_progress_card.html`.

    The template branches on `batch.status`; terminal-state markup OMITS
    `hx-trigger`/`hx-get` so HTMX halts polling automatically (Pitfall 6).

    phaze-xsje: a vanished row (deleted between polls, e.g. an operator delete racing the
    stall reaper) renders the template's terminal `gone` branch with a 200 status rather than
    raising 404. HTMX 2.x's default responseHandling does not swap non-2xx bodies into the DOM,
    so a 404 here would leave the previous RUNNING card's outerHTML poller armed and 404-polling
    forever; a 200 `gone` fragment replaces it and halts the poll.
    """
    batch = await session.get(ScanBatch, batch_id)
    if batch is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/scan_progress_card.html",
            context={"request": request, "gone": True},
        )
    agent = await session.get(Agent, batch.agent_id)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_progress_card.html",
        context={
            "request": request,
            "gone": False,
            "batch": batch,
            "agent_name": agent.name if agent is not None else batch.agent_id,
            "elapsed_seconds": elapsed_seconds(batch),
            "seconds_since_progress": seconds_since_progress(batch),
            "is_stalled": is_scan_stalled(batch),
        },
    )


async def build_recent_scans(session: AsyncSession, sort: SortState | None = None) -> list[ScanBatch]:
    """Query the last 10 non-LIVE ScanBatches and attach the transient UI attrs.

    Shared by ``pipeline.dashboard`` (initial render) and ``delete_scan`` (HTMX
    re-render after a delete) so the query + attribute attachment lives in exactly
    one place. Phase 27 gap-14: a duplicated elapsed-seconds copy carrying the
    pre-gap-12 tz-naive antipattern crashed the Recent Scans table the first time
    it loaded a real tz-aware row -- the shared helper prevents that regression.

    Attaches ``_agent_name``, ``_elapsed_seconds``, ``_seconds_since_progress`` and
    ``_is_stalled`` as transient attributes the template consumes (avoids N+1).
    The LIVE sentinel batches are excluded (UI-SPEC line 401).

    phaze-a6hm.6: ``sort`` is a resolved :class:`~phaze.routers.column_sort.SortState` (never a raw
    request string -- it has already passed the whitelist, so no key from the wire reaches a column
    here). It becomes the ORDER BY, which is what makes this sort SERVER-SIDE: the ordering is
    applied BEFORE the ``LIMIT 10``, so "the 10 most recent scans" and "the 10 longest paths" are
    genuinely different row SETS. Sorting the returned list in Python instead would only reorder the
    ten rows already chosen by created_at -- contract rule 1's exact failure, and here it would mean
    the operator sorting by Path never sees a row outside the ten newest.

    ``ScanBatch.id`` is appended as a tiebreaker so a sort on a low-cardinality column (status, or an
    agent with many scans) has a stable, deterministic order rather than one Postgres may vary
    between the poll and the render it replaces.

    ``sort=None`` keeps the historical ``created_at DESC`` ordering, so callers that never sort
    (and any future one) behave exactly as before.
    """
    order_by = (*sort.order_by(), ScanBatch.id) if sort is not None else (ScanBatch.created_at.desc(), ScanBatch.id)
    recent_scans_stmt = select(ScanBatch).where(ScanBatch.status != ScanStatus.LIVE.value).order_by(*order_by).limit(10)
    rows = list((await session.execute(recent_scans_stmt)).scalars().all())

    # One query for the id -> name map (avoids N+1). Include every agent so a scan
    # owned by a since-revoked agent still resolves to a human-readable name.
    name_result = await session.execute(select(Agent.id, Agent.name))
    # Comprehension (not dict(...)) because mypy cannot prove a Sequence[Row] is an
    # Iterable[tuple[str, str]]; ruff's C416 dict() rewrite is suppressed here.
    agent_name_by_id = {agent_id: name for agent_id, name in name_result.all()}  # noqa: C416

    # quick 260622-i0w: per-agent scanned/deduped/unique reconciliation, fetched ONCE so each row's
    # FILES cell can show "→ N unique · M deduped". get_agent_reconciliations owns the never-500
    # degrade (returns {} on any DB error → no annotations), so no try/except here. Attaching it in
    # the SHARED helper keeps dashboard() and delete_scan() in lockstep automatically.
    recon_by_agent = await get_agent_reconciliations(session)

    for batch in rows:
        batch._agent_name = agent_name_by_id.get(batch.agent_id, batch.agent_id)  # type: ignore[attr-defined]
        batch._elapsed_seconds = elapsed_seconds(batch) if batch.created_at else None  # type: ignore[attr-defined]
        batch._seconds_since_progress = seconds_since_progress(batch) if batch.created_at else None  # type: ignore[attr-defined]
        batch._is_stalled = is_scan_stalled(batch)  # type: ignore[attr-defined]
        batch._reconciliation = recon_by_agent.get(batch.agent_id)  # type: ignore[attr-defined]
    return rows


@router.delete("/{batch_id}", response_class=HTMLResponse)
async def delete_scan(
    request: Request,
    batch_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    sort: Annotated[str | None, Query()] = None,
    order: Annotated[str | None, Query()] = None,
    poll: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Delete a terminal scan + all associated DB data, then re-render the table.

    Guards (server-side authoritative -- defense-in-depth against a stale button
    or a reaper-flipped status). phaze-ytmfm: none of these raise anymore -- see the STATUS
    CONTRACT note above the guard block below; each renders the same re-rendered table with a
    ``role="alert"`` banner at :data:`~phaze.routers.response_shape.RENDERABLE_ALERT_STATUS`
    (200) instead, because a raw 4xx here would be silently dropped by htmx (response_shape.py
    rule 3) and this table's sole caller (the trash control in this table's own template) would
    never see it:
    - unknown batch -> "that scan is already gone" alert.
    - ``status == 'live'`` -> "cannot delete the live watcher" alert (it can NEVER be deleted).
    - non-terminal (``running``) -> "cannot delete a running scan" alert (only completed/failed
      scans are deletable).

    On a deletable row: run the ordered cascade, commit atomically, then return the
    re-rendered Recent Scans section for the HTMX ``outerHTML`` swap into
    ``#recent-scans``.

    phaze-a6hm.6: this is the THIRD producer of ``#recent-scans`` and it swaps ``outerHTML`` like
    the poll, so it carries ``sort``/``order`` for the same reason (column_sort rule 4a) -- deleting
    a row must not silently re-sort the table underneath the operator, and the copy it swaps in
    must keep polling in the chosen order rather than reverting one tick later.

    phaze-8f9j: it also carries ``poll`` for the mirror-image reason. The Discover workspace mounts
    this table poll-free (WORK-05: one chrome poll in the v7 shell) and its delete control appends
    ``poll=0``, so deleting a row there cannot swap in a copy that starts a second 5s loop. Absent
    (every other caller) still means poll.

    Until this bead this endpoint had NO caller: the delete control lives only in
    ``recent_scans_table.html``, which no served document mounted between the Phase-62 cutover and
    the workspace re-mount -- so this handler, ``delete_scan_cascade`` behind it, and the
    now-renderable guards above were reachable by curl alone, and an operator's only remediation
    for a half-ingested failed scan was psql.
    """
    batch = await session.get(ScanBatch, batch_id)
    alert_message: str | None = None
    if batch is None:
        alert_message = "That scan is already gone -- table refreshed."
    elif batch.status == ScanStatus.LIVE.value:
        alert_message = "The live watcher batch cannot be deleted."
    elif batch.status not in _TERMINAL_STATUSES:
        alert_message = "Cannot delete a running scan; wait for it to complete or fail."
    else:
        counts = await delete_scan_cascade(session, batch_id)
        await session.commit()
        logger.info("scan deleted", batch_id=str(batch_id), **counts)

    polling = poll != "0"
    sort_state = RECENT_SCANS_SORT.resolve(sort=sort, order=order, view_state={} if polling else {"poll": "0"})
    rows = await build_recent_scans(session, sort=sort_state)
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/recent_scans_table.html",
        context={"request": request, "recent_scans": rows, "sort": sort_state, "scans_poll": polling, "alert_message": alert_message},
        status_code=RENDERABLE_ALERT_STATUS,
    )
