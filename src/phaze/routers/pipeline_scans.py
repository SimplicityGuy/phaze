"""POST /pipeline/scans (admin trigger) + GET /pipeline/scans/{batch_id} (HTMX poll) + GET /pipeline/scans/agent-roots (agent-dropdown swap) -- Phase 27 D-05..D-08.

This is the operator-facing admin router (Wave 3) that closes SCAN-01. The operator
picks an agent + scan_root + optional subpath from the Trigger Scan card on the
`/pipeline/` dashboard, and this handler:

1. Validates the form server-side (T-27-03): joins root + subpath, NFC-normalizes,
   rejects literal `..` as a path *component* (see `trigger_scan` below), enforces
   prefix-against `agent.scan_roots`, and verifies the agent is not revoked.
2. Creates a RUNNING `ScanBatch` row.
3. Enqueues `scan_directory(scan_path, batch_id)` via the lifespan-wired
   `AgentTaskRouter.enqueue_for_agent` (Phase 26 D-19) to the chosen agent's
   per-agent SAQ queue.
4. Returns the in-progress `scan_progress_card.html` markup for HTMX swap into
   `#scan-submit-result`.

The poll endpoint (`GET /pipeline/scans/{batch_id}`) returns the same template
keyed off `batch.status`; terminal-state markup OMITS `hx-trigger`/`hx-get` so the
HTMX `outerHTML` swap halts polling automatically (Pitfall 6).

The agent-roots swap (`GET /pipeline/scans/agent-roots`) re-renders the
`scan_path_picker.html` partial with the chosen agent's `scan_roots` jsonb entries
populated; missing/revoked/empty agents render the yellow-surface empty state.
"""

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
import unicodedata
import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.column_sort import DESCENDING, SortableColumn, SortContract, SortState
from phaze.routers.response_shape import RENDERABLE_ALERT_STATUS
from phaze.schemas.agent_tasks import ScanDirectoryPayload
from phaze.schemas.pipeline_scans import TriggerScanForm
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.pg_text import contains_pg_invalid_chars
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


def _render_scan_alert(request: Request, error_message: str) -> HTMLResponse:
    """Render `scan_submit_error.html` -- THE swappable-alert envelope for every
    `trigger_scan` failure branch (phaze-u1gf, `routers/response_shape.py` rule 3).

    Every failure below is a *renderable alert*, not a 4xx/5xx: the Trigger Scan form
    posts with `hx-target="#scan-submit-result" hx-swap="innerHTML"`, so there is a
    swap target the operator is looking at right now -- which is exactly the test in
    contract rule 4 that separates this module from `request_guards` rule 1's 422.
    htmx 2.x's default `responseHandling` maps `[45]..` to `{swap: false, error: true}`,
    so a non-2xx status here would mean the `role="alert"` card was fetched and then
    DISCARDED: the operator would see the spinner flash, an empty `#scan-submit-result`,
    and no indication the scan was rejected. The error semantics live in the BODY
    (`role="alert"` + prose), not the status line -- hence
    :data:`~phaze.routers.response_shape.RENDERABLE_ALERT_STATUS` (200) here. Note this
    is NOT "errors are 200" in general -- a genuinely unintelligible envelope (e.g. a
    missing `agent_id` form field) is still FastAPI's own 422, because there is no
    meaningful answer to render into anything; that case never reaches this helper.
    """
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_submit_error.html",
        context={"request": request, "error_message": error_message},
        status_code=RENDERABLE_ALERT_STATUS,
    )


def _normalize_and_validate_scan_path(request: Request, form: TriggerScanForm) -> HTMLResponse | str:
    """Phase 27 D-06 + T-27-03, ordered: join, NUL-reject, ``..``-reject, THEN canonicalize.

    The order matters and must not change:

    1. NFC-normalize the joined `scan_root + '/' + subpath` (Pitfall 3).
    2. Reject NUL (U+0000) / lone surrogates in the joined path (phaze-jpji): PostgreSQL
       cannot store these in a UTF8 text column and none of the layers below happen to
       catch them. Reject explicitly rather than sanitize -- silently stripping the NUL
       (services.pg_text.sanitize_pg_text) would point the scan at a DIFFERENT
       filesystem path than the operator typed, which is worse than refusing outright.
    3. Reject literal ``..`` as a path *component*, not a substring (WR-01). The simple
       ``".." in joined`` rejected any legitimate filename containing the literal
       substring ``..`` (e.g., ``"...thinking.mp3"``, ``"Album...Live"``,
       ``"..notes/file.mp3"``). Splitting on path separators and asserting that no
       component is exactly ``..`` blocks the intended traversal pattern
       (``../../etc/passwd``) without those false-positives.
    4. Canonicalize (phaze-0wme) -- AFTER the ``..`` rejection above, never before:
       `PurePosixPath` would silently resolve ``..`` components if it ran first, making
       traversal invisible. `str(PurePosixPath(joined))` drops a trailing slash,
       duplicate internal slashes, and ``.`` components, so every spelling of the same
       directory (`"2026"`, `"2026/"`, `"/2026//"`, `"./2026"`) collapses to one string.
       This canonical value -- not the pre-canonical join -- is what feeds the prefix
       check, `ScanBatch.scan_path`, and therefore
       `uq_scan_batches_agent_id_scan_path_running` (migration 044, phaze-1a71): all
       three must agree on one canonical string or the partial-unique duplicate-dispatch
       guard can be bypassed by a differently-spelled resubmit of the same path.

    Returns the canonical joined path, or a rendered alert on rejection.
    """
    joined_raw = f"{form.scan_root.rstrip('/')}/{form.subpath.lstrip('/')}" if form.subpath else form.scan_root
    joined = unicodedata.normalize("NFC", joined_raw)

    if contains_pg_invalid_chars(joined):
        return _render_scan_alert(request, "Subpath must not contain NUL bytes or invalid Unicode.")

    if ".." in PurePosixPath(joined).parts:
        return _render_scan_alert(request, "Subpath must not contain '..' path traversal.")

    return str(PurePosixPath(joined))


async def _authorize_scan_root(request: Request, session: AsyncSession, form: TriggerScanForm, joined: str) -> HTMLResponse | Agent:
    """Look up the agent and authorize `joined` against its configured `scan_roots`.

    Server-side authoritative gate even though the dropdown filters revoked agents
    client-side (defensive per threat model "Revoked agent attempting to be selected
    via direct POST").

    phaze-g0if + phaze-0wme: `joined` is already NFC-normalized and canonicalized, but
    `agent.scan_roots` is stored verbatim -- nothing upstream (TriggerScanForm, the
    CLI's `add_agent`, or the JSONB column itself) normalizes it. A root configured in a
    non-NFC form (NFD is the norm for paths sourced from an HFS+/macOS agent, e.g.
    "Café" decomposed as "Cafe" + combining acute) or with a trailing slash (e.g.
    "/archive/") then byte-differs from its own normalized/collapsed self, so a raw
    membership/prefix check can spuriously fail for a legitimately configured,
    un-traversed scan. Normalize + canonicalize every `r` (and `scan_root` itself) the
    same way `joined` was, so the membership gate, the prefix gate, and the persisted
    `scan_path` all agree on one normalization form.

    WR-05: the form-submitted ``scan_root`` MUST itself be one of the agent's configured
    ``scan_roots`` (literal membership) -- previously only the joined
    ``scan_root + '/' + subpath`` was validated against the prefix list, which allowed a
    partial match like ``scan_root="/data"`` + ``subpath="music/foo"`` to authorize
    ``/data/music/foo`` even though ``/data`` itself was never configured.

    D-06 prefix validation then confirms `joined` matches (or descends from) one of the
    agent's configured scan_roots, stripping the trailing slash on roots so
    `"/data/music"` matches both `"/data/music"` and `"/data/music/2026"`.

    Returns the authorized `Agent`, or a rendered alert on rejection.
    """
    agent = await session.get(Agent, form.agent_id)
    if agent is None or agent.revoked_at is not None:
        return _render_scan_alert(request, "Unknown or revoked agent.")

    scan_root_nfc = str(PurePosixPath(unicodedata.normalize("NFC", form.scan_root)))
    scan_roots_nfc = [str(PurePosixPath(unicodedata.normalize("NFC", r))) for r in agent.scan_roots]

    if scan_root_nfc not in scan_roots_nfc:
        return _render_scan_alert(request, "Selected scan root is not configured for this agent.")

    if not any(joined == r or joined.startswith(r.rstrip("/") + "/") for r in scan_roots_nfc):
        return _render_scan_alert(request, "Resolved path is outside the selected scan root.")

    return agent


async def _insert_running_scan_batch(request: Request, session: AsyncSession, form: TriggerScanForm, joined: str) -> HTMLResponse | ScanBatch:
    """Create + commit a RUNNING ScanBatch (D-08 + D-14), translating the duplicate guard.

    phaze-1a71: the insert is guarded by `uq_scan_batches_agent_id_scan_path_running`
    (migration 044) -- the durable, race-safe duplicate-dispatch guard. A read-then-insert
    check on the Python side is a TOCTOU (two concurrent submits can both pass a read
    before either commits), so the database enforces "at most one RUNNING batch per
    (agent_id, scan_path)" atomically at insert time instead. A double submit (a slow
    first request re-clicked, or a re-submit an hour into a scan that looks stalled)
    fails HERE with an `IntegrityError`, which this translates into the same alert shape
    as every other rejection rather than dispatching a second concurrent, unbounded full
    SHA-256 archive walk of the same tree. Do not replace this with a racy pre-check.

    phaze-266lc: no ``session.refresh(batch)`` after commit. The sessionmaker is
    ``expire_on_commit=False`` (database.py), so ``batch``'s attributes already survive
    the commit without a refresh -- refreshing was redundant and its sole effect was to
    autobegin a NEW transaction on this session, which then sat idle-in-transaction
    across the enqueue call that follows (the phaze-1v37 pool-drain class: PgBouncer
    SESSION mode pins an upstream server slot per checkout, and a long-open idle
    transaction also holds back the vacuum xmin horizon).

    Returns the committed `ScanBatch`, or a rendered alert if the duplicate guard fired.
    """
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id=form.agent_id,
        scan_path=joined,
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
        # PR4: a freshly-created RUNNING batch starts with a heartbeat so the
        # stall reaper does not immediately consider it stalled.
        last_progress_at=datetime.now(UTC),
    )
    session.add(batch)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _render_scan_alert(request, "A scan is already running for this path.")
    return batch


async def _enqueue_scan_or_mark_failed(
    request: Request,
    session: AsyncSession,
    batch: ScanBatch,
    form: TriggerScanForm,
    joined: str,
) -> HTMLResponse | None:
    """Enqueue `scan_directory` via AgentTaskRouter (Phase 26 D-19); reconcile failure.

    scan_directory is a long-running BULK archive walk (a full SHA-256 walk of a large
    network-mounted archive legitimately takes 1-2h). A fixed SAQ wall-clock timeout
    would kill a healthy, progressing scan and -- because SAQ retries restart the job
    FROM SCRATCH with no checkpoint -- loop forever, so a full scan could never
    complete. Disable the wall-clock timeout (timeout=0 -> wait_for(..., None)
    unbounded; Job.stuck stays False so the saq sweep never reaps it either) and disable
    retries (a restart-from-zero retry is wasteful; the operator re-triggers). Liveness
    is enforced by the progress-based stall reaper (config.scan_stall_seconds).

    Two failure shapes, both handled without letting an exception escape the handler:

    - **Ambiguous** (`AmbiguousEnqueueError`, phaze-0dfj4): raised AFTER the broker
      connection was already live -- the ``saq_jobs`` INSERT may already be durably
      committed even though this call never got its ack (a connection drop AFTER the
      server-side commit is the textbook in-doubt transaction). Marking the batch FAILED
      here would be a lie the operator acts on: they re-trigger, the uq constraint only
      covers RUNNING rows so a second batch + job is created, and the phantom first job
      ALSO dequeues -- walking the archive tree concurrently with the real scan before
      crash-looping on its first PATCH against the now-terminal batch. Leave the batch
      RUNNING instead (it was already committed RUNNING) and fall through to the same
      progress-card render a confirmed-successful enqueue gets: a genuinely-lost enqueue
      then has no agent that will ever report progress, and the progress-based stall
      reaper resolves it the same way it resolves an agent that silently died mid-scan --
      the same non-terminal contract phaze-9f82r established for an ambiguous tag-write
      enqueue.
    - **Definite** (any other `Exception`): mark the batch FAILED. WR-06: previously the
      failure path called ``session.delete(batch)`` + ``session.commit()``, but if THAT
      also raised (same network issue that broke the enqueue could have taken Postgres
      out, or the session was now in a tainted state), the exception escaped the handler
      -- FastAPI returned a generic 500 (losing the documented copy) and the orphan
      RUNNING ScanBatch row stayed visible to Recent Scans forever (no agent would ever
      PATCH it because nothing was enqueued). Marking FAILED instead of deleting is more
      honest: the operator sees a FAILED row in Recent Scans with a clear
      error_message. The secondary commit is wrapped in its own try/except so a
      Postgres-down scenario still produces the alert envelope instead of bubbling to a
      500 -- the operator's alert is more important than the orphan-row cleanup, and the
      original cause is already logged.

    Returns None on success (including the ambiguous case, batch left RUNNING), or a
    rendered alert on definite failure.
    """
    try:
        await request.app.state.task_router.enqueue_for_agent(
            agent_id=form.agent_id,
            task_name="scan_directory",
            payload=ScanDirectoryPayload(scan_path=joined, batch_id=batch.id, agent_id=form.agent_id),
            timeout=0,
            retries=0,
        )
    except AmbiguousEnqueueError:
        logger.error(
            "scan trigger: enqueue ambiguous for batch=%s -- broker connection was live, job may "
            "have landed; leaving batch RUNNING for the stall reaper to resolve",
            batch.id,
            exc_info=True,
        )
        return None
    except Exception:
        logger.exception("scan trigger: enqueue failed for batch=%s; marking FAILED", batch.id)
        batch.status = ScanStatus.FAILED.value
        batch.error_message = "controller could not enqueue scan to agent worker"
        try:
            await session.commit()
        except Exception:
            # Don't let a rollback-commit failure escape the handler; the
            # operator's alert envelope is more important than the orphan-row
            # cleanup, and we already logged the original cause above.
            logger.exception("scan trigger: secondary commit failed for batch=%s", batch.id)
            await session.rollback()
        return _render_scan_alert(request, "The application server could not enqueue the scan. Try again in a moment.")
    return None


@router.post("", response_class=HTMLResponse)
async def trigger_scan(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    # phaze-oldp: mirrors the sibling agent_roots_swap's `Query(pattern=..., max_length=128)`
    # (agent-roots swap target above), which is itself the wire mirror of the
    # `CheckConstraint("id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'")` on Agent.id (models/agent.py). Without
    # a signature-level bound, a NUL-bearing agent_id (never a valid id -- it can never match the
    # constraint) reached `session.get(Agent, form.agent_id)` below unfiltered and 500'd: Postgres
    # cannot bind NUL in a UTF8 text comparison at all, even to fail to match. A stricter
    # signature rejects it as a 422 at the boundary instead (this handler's own docstring carves
    # a 422 out for "a genuinely unintelligible envelope" -- an id that can never denote a real
    # agent is exactly that, unlike scan_root/subpath which stay free-form Form() since they are
    # validated against agent.scan_roots content, not a charset).
    agent_id: Annotated[str, Form(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)],
    scan_root: Annotated[str, Form()],
    subpath: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Form submit: validate, create ScanBatch, enqueue `scan_directory`.

    A thin coordinator over the ordered phases below -- each phase either returns the
    canonical value the next phase needs, or a rendered `scan_submit_error.html` alert
    that short-circuits the request:

    1. :func:`_normalize_and_validate_scan_path` -- NFC-normalize, reject NUL/invalid
       Unicode (phaze-jpji), reject `..` traversal (WR-01), canonicalize (phaze-0wme).
    2. :func:`_authorize_scan_root` -- look up the agent (reject missing/revoked),
       enforce literal `scan_root` membership (WR-05) and D-06 prefix containment.
    3. :func:`_insert_running_scan_batch` -- create + commit a RUNNING ScanBatch,
       translating the partial-unique duplicate guard (phaze-1a71) into an alert.
    4. :func:`_enqueue_scan_or_mark_failed` -- enqueue `scan_directory`, reconciling an
       ambiguous enqueue (leave RUNNING) vs a definite failure (mark FAILED).

    On success: render `scan_progress_card.html` in RUNNING state for HTMX swap into
    `#scan-submit-result`. See :func:`_render_scan_alert` for the STATUS CONTRACT
    governing every failure branch (phaze-u1gf, `routers/response_shape.py` rule 3).
    """
    form = TriggerScanForm(agent_id=agent_id, scan_root=scan_root, subpath=subpath)

    joined = _normalize_and_validate_scan_path(request, form)
    if isinstance(joined, HTMLResponse):
        return joined

    agent = await _authorize_scan_root(request, session, form, joined)
    if isinstance(agent, HTMLResponse):
        return agent

    batch = await _insert_running_scan_batch(request, session, form, joined)
    if isinstance(batch, HTMLResponse):
        return batch

    enqueue_alert = await _enqueue_scan_or_mark_failed(request, session, batch, form, joined)
    if enqueue_alert is not None:
        return enqueue_alert

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/scan_progress_card.html",
        context={
            "request": request,
            "batch": batch,
            "agent_name": agent.name,
            "elapsed_seconds": 0,
            # Freshly-created batch: it just stamped last_progress_at, so it is
            # 0s since progress and never stalled.
            "seconds_since_progress": 0,
            "is_stalled": False,
        },
    )
