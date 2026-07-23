"""Tracklists admin UI router -- 1001Tracklists and fingerprint tracklist management page."""

from collections import defaultdict
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from saq import Status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError
import structlog

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers.cue import _get_cue_version
from phaze.routers.response_shape import wants_fragment
from phaze.schemas.agent_tasks import ScanLiveSetPayload
from phaze.services.cue_generator import parse_timestamp_string
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, resolve_queue_for_task, resolve_queues_for_owned_files
from phaze.services.proposal_queries import Pagination
from phaze.services.stage_status import is_applied
from phaze.services.tracklist_matcher import compute_match_confidence, parse_live_set_filename
from phaze.services.tracklist_scraper import TracklistScraper
from phaze.tasks.tracklist import _store_scraped_tracklist


logger = structlog.get_logger(__name__)

EDITABLE_FIELDS = {"artist", "title", "timestamp"}

# wire_bounds rule 1: a string field's cap equals its mapped ``String(N)`` column width. ``artist``
# and ``title`` land in ``TracklistTrack.artist``/``.title`` -- both ``Text``, unbounded, no entry
# needed (rule 2). ``timestamp`` lands in ``TracklistTrack.timestamp String(20)`` (models/tracklist.py)
# and is the only editable field that needs a guard (phaze-81au). This mirrors the cap already
# applied on the agent-write path, ``TracklistTrackPayload.timestamp`` (schemas/agent_tracklists.py,
# phaze-btlu) -- both sides of the same column agree on 20.
EDITABLE_FIELD_MAX_LENGTHS = {"timestamp": 20}

# The client submits a search result's own url so link_search_result can scrape it (phaze-ldal).
# That form field is attacker-controllable, so before ever handing it to httpx we pin it to the
# exact host the scraper is built for -- otherwise the server becomes an open fetch proxy (SSRF)
# for whatever internal/external URL a tampered request supplies.
_ALLOWED_TRACKLIST_HOST = urlparse(TracklistScraper.BASE_URL).netloc


def _is_allowed_tracklist_url(url: str) -> bool:
    """True if url is an https URL on the 1001Tracklists host the scraper is scoped to."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == _ALLOWED_TRACKLIST_HOST


async def _file_match_context(session: AsyncSession, file_id: uuid.UUID | None) -> tuple[str | None, str | None, Any]:
    """Derive (artist, event, date) signals to score a tracklist against a FILE.

    Mirrors the ``search_tracklist`` task heuristic (phaze-ldal): parse the v1.0 live-set
    filename pattern first, falling back to tag metadata. Returns all-None when there's no
    linked file to compare against, rather than falling back to a tracklist's own fields --
    scoring a tracklist against itself always looks like a perfect match.
    """
    if file_id is None:
        return None, None, None

    result = await session.execute(select(FileRecord).options(selectinload(FileRecord.file_metadata)).where(FileRecord.id == file_id))
    file_record = result.scalar_one_or_none()
    if file_record is None:
        return None, None, None

    parsed = parse_live_set_filename(file_record.original_filename)
    if parsed:
        return parsed

    file_artist = file_record.file_metadata.artist if file_record.file_metadata else None
    return file_artist, None, None


async def _has_candidates(session: AsyncSession, tracklist: Tracklist) -> bool:
    """Check if any track in this tracklist has candidate DiscogsLinks."""
    if not tracklist.latest_version_id:
        return False
    track_ids_stmt = select(TracklistTrack.id).where(TracklistTrack.version_id == tracklist.latest_version_id)
    exists_stmt = select(func.count(DiscogsLink.id)).where(
        DiscogsLink.track_id.in_(track_ids_stmt),
        DiscogsLink.status == "candidate",
    )
    result = await session.execute(exists_stmt)
    return (result.scalar() or 0) > 0


async def _attach_track_count(session: AsyncSession, tracklist: Tracklist) -> None:
    """Set ``tracklist._track_count`` so the tracklist_card.html badge reads the real count.

    tracklist_card.html renders ``{{ tracklist._track_count if tracklist._track_count is defined
    else 0 }} tracks`` -- a dynamic, non-mapped attribute (phaze-y7ez) that the list renders
    (list_tracklists / _render_tracklist_list) populate on every ORM object they hand to the
    template, but that a bare ``Tracklist`` fetched fresh by a single-card mutation route lacks.
    Every route that re-renders tracklist_card.html as its response MUST call this first, or the
    swapped-in card falsely reads "0 tracks" regardless of the real count (indistinguishable from
    data loss to the operator).
    """
    if tracklist.latest_version_id:
        count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tracklist.latest_version_id))
        tracklist._track_count = count_result.scalar() or 0  # type: ignore[attr-defined]
    else:
        tracklist._track_count = 0  # type: ignore[attr-defined]


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/tracklists", tags=["tracklists"])

AUDIO_EXTENSIONS = {"mp3", "m4a", "ogg", "flac", "wav", "opus", "aac"}


async def _render_discogs_candidates(request: Request, session: AsyncSession, track_id: uuid.UUID, *, status_code: int = 200) -> HTMLResponse:
    """Re-query and render the non-dismissed Discogs candidate panel for a track.

    Shared by the accept/dismiss success paths and the phaze-xdu1 concurrent-change recovery path:
    when match_tracklist_to_discogs's short candidate-swap transaction commits between a router
    SELECT and its UPDATE, the ORM write matches 0 rows and raises StaleDataError. Rather than let
    that escape as a 500, the caller rolls back and re-renders the CURRENT candidate set (a friendly
    'candidates changed, refresh') so the operator simply sees the freshly matched candidates.
    """
    stmt = select(DiscogsLink).where(DiscogsLink.track_id == track_id, DiscogsLink.status != "dismissed").order_by(DiscogsLink.confidence.desc())
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())
    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/discogs_candidates.html",
        context={"request": request, "track_id": str(track_id), "candidates": candidates},
        status_code=status_code,
    )


async def _get_tracklist_stats(session: AsyncSession) -> dict[str, int]:
    """Query aggregate tracklist counts: total, matched, unmatched, proposed."""
    total_result = await session.execute(select(func.count(Tracklist.id)))
    total = total_result.scalar() or 0

    matched_result = await session.execute(select(func.count(Tracklist.id)).where(Tracklist.file_id.is_not(None)))
    matched = matched_result.scalar() or 0

    proposed_result = await session.execute(select(func.count(Tracklist.id)).where(Tracklist.status == "proposed"))
    proposed = proposed_result.scalar() or 0

    return {"total": total, "matched": matched, "unmatched": total - matched, "proposed": proposed}


async def _get_tracklist_count(session: AsyncSession, filter_value: str) -> int:
    """Count tracklists by filter type."""
    stmt = select(func.count(Tracklist.id))
    if filter_value == "matched":
        stmt = stmt.where(Tracklist.file_id.is_not(None))
    elif filter_value == "unmatched":
        stmt = stmt.where(Tracklist.file_id.is_(None))
    elif filter_value == "proposed":
        stmt = stmt.where(Tracklist.status == "proposed")
    result = await session.execute(stmt)
    return result.scalar() or 0


@router.get("/", response_class=HTMLResponse)
async def list_tracklists(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    filter: str = Query("all"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the tracklists list page or HTMX partial."""
    # SHELL-05 (D-03): a plain (non-HX) GET / bookmark resolves into the v7.0 shell.
    # The in-page HX filter branch below is left intact so the app stays usable (D-01).
    #
    # phaze-64uy (HYGIENE for the shape branch, a REAL FIX for the template flag below):
    # ``wants_fragment`` per response_shape.py contract rule 1, matching the sibling
    # ``/tracklists/scan`` handler converted by phaze-xc84. ``tracklists/partials/pagination.html``
    # issues ``/tracklists/?filter=...&page=...`` WITHOUT ``hx-push-url``, so no URL enters
    # history and no restore can reach this handler today.
    is_fragment = wants_fragment(request)
    if not is_fragment:
        return RedirectResponse(url="/s/tracklist", status_code=302)

    stmt = select(Tracklist)

    if filter == "matched":
        stmt = stmt.where(Tracklist.file_id.is_not(None))
    elif filter == "unmatched":
        stmt = stmt.where(Tracklist.file_id.is_(None))
    elif filter == "proposed":
        stmt = stmt.where(Tracklist.status == "proposed")

    # ``match_confidence`` and ``created_at`` are both non-unique, so two tracklists can tie on the
    # FULL compound key; with a partial ORDER BY the OFFSET/LIMIT boundary is arbitrary on such a
    # tie (heap order, which shifts with page layout, vacuum, and plan choice) -- the same
    # stable-pagination bug fixed for ``scan_tab`` (phaze-rgxg): a tied row can appear on TWO
    # consecutive pages, or be skipped entirely between page N and N+1. Appending the unique
    # ``Tracklist.id`` (DESC, matching the descending secondary sort) makes the order TOTAL, so
    # every page boundary is deterministic across repeated calls. Same rationale as the paging
    # contract's mandatory unique tiebreaker (rule 4, see :mod:`phaze.services.pagination`).
    stmt = stmt.order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.created_at.desc(), Tracklist.id.desc())

    # Count total for pagination
    total = await _get_tracklist_count(session, filter)
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)

    result = await session.execute(stmt)
    tracklists = list(result.scalars().all())

    # Load track counts for each tracklist via latest version
    for tl in tracklists:
        if tl.latest_version_id:
            count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tl.latest_version_id))
            tl._track_count = count_result.scalar() or 0  # type: ignore[attr-defined]
        else:
            tl._track_count = 0  # type: ignore[attr-defined]

    # Compute has_candidates for each tracklist
    for tl in tracklists:
        if tl.latest_version_id:
            track_ids_stmt = select(TracklistTrack.id).where(TracklistTrack.version_id == tl.latest_version_id)
            cand_result = await session.execute(
                select(func.count(DiscogsLink.id)).where(
                    DiscogsLink.track_id.in_(track_ids_stmt),
                    DiscogsLink.status == "candidate",
                )
            )
            tl._has_candidates = (cand_result.scalar() or 0) > 0  # type: ignore[attr-defined]
        else:
            tl._has_candidates = False  # type: ignore[attr-defined]

    # Compute CUE version for approved tracklists with executed files
    for tl in tracklists:
        if tl.status == "approved" and tl.file_id:
            fr_result = await session.execute(select(FileRecord).where(FileRecord.id == tl.file_id))
            fr = fr_result.scalar_one_or_none()
            if fr and await is_applied(session, fr.id):
                tl._cue_version = _get_cue_version(fr.current_path)  # type: ignore[attr-defined]
            else:
                tl._cue_version = 0  # type: ignore[attr-defined]
        else:
            tl._cue_version = 0  # type: ignore[attr-defined]

    stats = await _get_tracklist_stats(session)
    pagination = Pagination(page=page, page_size=page_size, total=total)

    context: dict[str, Any] = {
        "request": request,
        "tracklists": tracklists,
        "stats": stats,
        "pagination": pagination,
        "current_page": "tracklists",
        "active_filter": filter,
        # phaze-k2lz: this is NOT scan_tab's shape, despite the surface resemblance (phaze-64uy
        # copied the mechanism without the precondition holding). scan_tab has a real non-fragment
        # branch (``scan.html``) that establishes ``#scan-panel`` before any in-page swap ever fires,
        # so passing ``is_fragment`` straight through as ``is_hx`` is correct there: a live swap
        # really is landing inside an existing wrapper. Here the non-fragment branch 302-redirects
        # (SHELL-05/D-03) instead of rendering, so this handler's ONLY reachable path IS the fragment
        # one -- there is no earlier render that ever emits ``#tracklists-list``. Passing ``is_fragment``
        # here therefore suppressed the wrapper on every single reachable call, so the Prev/Next in
        # pagination.html and the Unlink/Link-result buttons in tracklist_card.html -- all hx-target="#tracklists-list"
        # -- had no landing target anywhere in the document: htmx logged a target error and no-op'd,
        # exactly the D-01 "in-page HX filter" the SHELL-05 contract promises stays usable. This handler
        # must always self-establish the wrapper; the ``_render_tracklist_list`` helper used by the
        # POST mutation routes (unlink/link-result) is untouched and still correctly suppresses it, since
        # those genuinely DO land inside the wrapper THIS render just created.
        "is_hx": False,
    }

    # CUT-02 (Phase 62): the non-HX path already 302-redirected above (SHELL-05), so this is
    # reached only for HX rail swaps -- the LIVE shell pagination/filter/sort fragment (D-03b).
    return templates.TemplateResponse(request=request, name="tracklists/partials/tracklist_list.html", context=context)


@router.get("/scan", response_class=HTMLResponse)
async def scan_tab(
    request: Request,
    page: int = Query(1, ge=1),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the scan tab content with unscanned audio files."""
    page_size = 20

    # Subquery: file IDs that already have a fingerprint-sourced tracklist
    scanned_subquery = select(Tracklist.file_id).where(Tracklist.source == "fingerprint").where(Tracklist.file_id.is_not(None)).correlate(FileRecord)

    # Audio files not yet scanned
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(AUDIO_EXTENSIONS),
        FileRecord.id.not_in(scanned_subquery),
    )

    # Count total unscanned
    count_stmt = select(func.count(FileRecord.id)).where(
        FileRecord.file_type.in_(AUDIO_EXTENSIONS),
        FileRecord.id.not_in(scanned_subquery),
    )
    count_result = await session.execute(count_stmt)
    total = count_result.scalar() or 0

    # Paginate
    #
    # ``original_filename`` carries no uniqueness constraint, so two files can share a value; with
    # a partial ORDER BY the OFFSET/LIMIT boundary is arbitrary on a tie (heap order, which shifts
    # with page layout, vacuum, and plan choice). Unlike a bare display LIMIT, this is a stable-
    # pagination bug: a tied row can appear on TWO consecutive pages, or be skipped entirely between
    # page N and N+1, because Postgres is free to reorder the tied block differently per query.
    # Appending the unique ``FileRecord.id`` (ASC, matching the ascending filename sort) makes the
    # order TOTAL, so every page boundary is deterministic across repeated calls. Same rationale as
    # the paging contract's mandatory unique tiebreaker (rule 4, see :mod:`phaze.services.pagination`).
    offset = (page - 1) * page_size
    stmt = stmt.order_by(FileRecord.original_filename, FileRecord.id).offset(offset).limit(page_size)

    result = await session.execute(stmt)
    files = list(result.scalars().all())

    pagination = Pagination(page=page, page_size=page_size, total=total)

    # Shape decision per routers/response_shape.py. ``wants_fragment`` is the ONLY sanctioned way
    # to ask (rule 1); branching on the raw ``HX-Request`` header here would answer a history
    # restore -- which also carries that header -- with a chrome-less fragment that htmx swaps
    # into <body>, ignoring hx-target (rule 2).
    #
    # ``is_hx`` is ALSO what scan_tab.html gates its ``#scan-panel`` wrapper on, and the two
    # decisions are the same decision: a live swap is landing INSIDE the existing wrapper and must
    # not carry a second one, while every full-document shape must supply exactly one. Passing the
    # same predicate to both keeps the id count at exactly one on every path.
    is_fragment = wants_fragment(request)
    context: dict[str, Any] = {
        "request": request,
        "files": files,
        "pagination": pagination,
        "total_unscanned": total,
        "is_hx": is_fragment,
        "current_page": "tracklists",
    }

    if is_fragment:
        return templates.TemplateResponse(request=request, name="tracklists/partials/scan_tab.html", context=context)

    return templates.TemplateResponse(request=request, name="tracklists/scan.html", context=context)


@router.post("/scan", response_class=HTMLResponse)
async def trigger_scan(
    request: Request,
    file_ids: list[str] = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Trigger batch fingerprint scanning for selected files, each on its OWNING agent (phaze-c9w9)."""
    # Parse submitted ids into UUIDs, skipping malformed strings (never a 500).
    parsed_ids: list[uuid.UUID] = []
    for fid in file_ids:
        try:
            parsed_ids.append(uuid.UUID(fid))
        except ValueError:
            continue

    # Resolve the matching FileRecords so each enqueue carries the original_path.
    records_result = await session.execute(select(FileRecord).where(FileRecord.id.in_(parsed_ids)))
    records = {record.id: record for record in records_result.scalars()}
    ordered_records = [records[file_id] for file_id in parsed_ids if file_id in records]

    try:
        # phaze-c9w9: group the selection by each file's OWNING agent and route per group --
        # never one most-recently-seen pick for the whole batch (a file whose owner is offline
        # is skipped, not rerouted onto a different agent's mount).
        routed_groups, skipped_records = await resolve_queues_for_owned_files("scan_live_set", request.app.state, session, ordered_records)
    except NoActiveAgentError:
        # No owning file-server agent is online; surface a visible empty-state and enqueue nothing.
        return templates.TemplateResponse(
            request=request,
            name="tracklists/partials/scan_progress.html",
            context={
                "request": request,
                "job_ids": "",
                "agent_id": "",
                "total": len(file_ids),
                "completed": 0,
                "done": True,
                "tracklists_created": 0,
                "no_active_agent": True,
            },
        )

    if skipped_records:
        logger.warning("trigger_scan: owning agent offline -- files skipped", skipped=len(skipped_records))

    job_ids: list[str] = []
    agent_ids: list[str] = []
    for routed, group in routed_groups:
        # scan_live_set is an AGENT_TASK, so each routed group carries a non-None agent_id;
        # cast narrows str | None -> str for ScanLiveSetPayload.
        agent_id = cast("str", routed.agent_id)
        agent_ids.append(agent_id)
        for record in group:
            # phaze-wsuf: use current_path, NOT original_path. A live-set file that already had a
            # rename/move proposal EXECUTED has its original_path pointing at a path execution
            # deleted; current_path is the field the system maintains as the file's live on-disk
            # location (equal to original_path until a move). Scanning original_path targets a
            # deleted path for an executed file -- either a hard failure or a false-negative clean
            # "no_matches" COMPLETE, permanently unscannable.
            payload = ScanLiveSetPayload(file_id=record.id, original_path=record.current_path, agent_id=agent_id)
            job = await routed.queue.enqueue("scan_live_set", **payload.model_dump(mode="json"))
            if job is not None:
                job_ids.append(job.key)

    # phaze-jdt4: zero enqueued jobs must render the TERMINAL state (done=True), not the polling
    # state. job_ids ends up empty whenever every submitted id was skipped -- a malformed UUID
    # (above), a FileRecord deleted/deduped between the scan-tab render and this submit, or every
    # `queue.enqueue` returning None -- and with done=False the progress partial's polling div
    # (`hx-get=".../scan/status?job_ids=&agent_id=..."`, `hx-trigger="every 3s"`) hits
    # `scan_status`'s `job_ids: str = Query(..., min_length=1)`, which 422s forever: HTMX never
    # swaps on a 4xx, so the panel is stuck on "Scanning... (0 of 0 files)" indefinitely. The
    # completion formula `completed >= total` (scan_status) is trivially True for total=0, so the
    # terminal state is rendered directly here instead, mirroring the no_active_agent branch above.
    done = len(job_ids) == 0

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/scan_progress.html",
        context={
            "request": request,
            "job_ids": ",".join(job_ids),
            # phaze-c9w9: the poll may now span multiple owning agents' lane queues -- thread the
            # DISTINCT routed agent ids (comma-separated) through to scan_status.
            "agent_id": ",".join(agent_ids),
            "total": len(job_ids),
            "completed": 0,
            "done": done,
            "tracklists_created": 0,
            "no_active_agent": False,
        },
    )


def _scan_failure_identifier(job: Any) -> str:
    """Return a human-readable identifier for a FAILED ``scan_live_set`` job.

    Prefers the enqueued ``original_path`` (available via ``job.kwargs`` --
    ``trigger_scan`` enqueues ``ScanLiveSetPayload``, which always carries
    ``original_path``) since that is what an operator recognizes in the UI.
    Falls back to the first line of ``job.error`` (SAQ's traceback string,
    where a FAILED job's failure detail actually lives -- ``job.result`` is
    always ``None`` on failure) and finally to a generic placeholder if
    neither is available.
    """
    kwargs = job.kwargs or {}
    original_path = kwargs.get("original_path")
    if original_path:
        return str(original_path)
    if job.error:
        return str(job.error).splitlines()[0]
    return "unknown"


@router.get("/scan/status", response_class=HTMLResponse)
async def scan_status(
    request: Request,
    job_ids: str = Query(..., min_length=1),
    agent_id: str = Query(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*(,[a-z0-9]+(-[a-z0-9]+)*)*$", max_length=128),
) -> HTMLResponse:
    """Poll scan progress by checking SAQ job results on the per-agent queue(s).

    ``scan_live_set`` jobs are enqueued onto each owning agent's meta lane
    (``phaze-agent-<id>-meta``), and ``queue.job(job_key)`` lookups are
    queue-scoped, so the poll must target the SAME lane queue each job was enqueued
    on (quick-260707-dh1). phaze-c9w9: ``trigger_scan`` routes each file to its
    OWNING agent, so a batch can span several agents; ``agent_id`` is the
    comma-separated distinct routed agent ids echoed through the progress partial,
    and each job key is looked up across those lane queues (a miss on one queue is
    ``None``; the first hit wins).

    COMPLETE and FAILED are handled as SEPARATE branches (not folded into one
    ``job.status in (...)`` check) because SAQ only populates ``job.result`` from
    the task's return value on a genuine COMPLETE; a FAILED job's ``result`` is
    always ``None`` -- its failure detail lives in ``job.error`` (the traceback
    string). ``scan_live_set`` (``tasks/scan.py``) never returns a
    ``{"status": "error"}`` result -- it only returns ``"no_matches"`` /
    ``"scanned"``; every failure path raises and becomes ``Status.FAILED``. So a
    FAILED job is reported explicitly as an error (surfacing the enqueued
    ``original_path`` when available, else the first line of ``job.error``)
    instead of being silently folded into ``completed`` with no error entry.
    """
    lane = lane_for_task("scan_live_set")
    queues = [request.app.state.task_router.queue_for(aid, lane) for aid in agent_id.split(",")]
    ids = [jid.strip() for jid in job_ids.split(",") if jid.strip()]

    completed = 0
    tracklists_created = 0
    errors: list[str] = []

    for job_key in ids:
        job = None
        for queue in queues:
            job = await queue.job(job_key)
            if job is not None:
                break
        if job is None:
            completed += 1
            continue
        if job.status == Status.FAILED:
            completed += 1
            errors.append(_scan_failure_identifier(job))
        elif job.status == Status.COMPLETE:
            completed += 1
            result_data = job.result
            if isinstance(result_data, dict) and result_data.get("status") == "scanned":
                tracklists_created += 1

    total = len(ids)
    done = completed >= total

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/scan_progress.html",
        context={
            "request": request,
            "job_ids": job_ids,
            "agent_id": agent_id,
            "total": total,
            "completed": completed,
            "done": done,
            "tracklists_created": tracklists_created,
            "errors": errors,
            "no_active_agent": False,
        },
    )


@router.get("/{tracklist_id}/tracks", response_class=HTMLResponse)
async def get_tracks(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return track detail partial for a tracklist's latest version."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()

    tracks: list[TracklistTrack] = []
    if tracklist and tracklist.latest_version_id:
        version_result = await session.execute(
            select(TracklistVersion).options(selectinload(TracklistVersion.tracks)).where(TracklistVersion.id == tracklist.latest_version_id)
        )
        version = version_result.scalar_one_or_none()
        if version:
            tracks = sorted(version.tracks, key=lambda t: t.position)

    if tracklist and tracklist.source == "fingerprint":
        template_name = "tracklists/partials/fingerprint_track_detail.html"
        context: dict[str, Any] = {"request": request, "tracklist": tracklist, "tracks": tracks}
    else:
        template_name = "tracklists/partials/track_detail.html"
        context = {"request": request, "tracks": tracks, "tracklist_id": tracklist_id}

    return templates.TemplateResponse(request=request, name=template_name, context=context)


@router.post("/{tracklist_id}/link", response_class=HTMLResponse)
async def link_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    file_id: uuid.UUID = Form(...),
    # confidence is a 0-100 match score (Tracklist.match_confidence Integer) -- the domain bound
    # beats the int32 column fallback (wire_bounds rule 3): a value outside 0-100 is nonsense even
    # when it fits in int4, and a value outside int4 raised NumericValueOutOfRange unhandled (phaze-k5ac).
    confidence: int = Form(..., ge=0, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Link a search result to a file."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        # phaze-29bv: file_id is a client-supplied FK to files.id. A well-formed but stale/forged id
        # -- e.g. the FileRecord hard-deleted by a concurrent scan while this search panel stayed open
        # -- passes wire validation and would detonate as an unhandled ForeignKeyViolation at commit,
        # poisoning the request transaction with a 500 and leaving the tracklist silently unlinked.
        # This is the render-vs-POST race request_guards.py rule 4 governs: resolve the FileRecord
        # first and branch cleanly rather than writing an unvalidated FK.
        if await session.get(FileRecord, file_id) is None:
            return HTMLResponse(content="File not found", status_code=404)
        tracklist.file_id = file_id
        tracklist.match_confidence = confidence
        tracklist.auto_linked = False
        await session.commit()

    return await _render_tracklist_list(request, session, "all")


@router.post("/{tracklist_id}/unlink", response_class=HTMLResponse)
async def unlink_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Unlink a tracklist from its file."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        tracklist.file_id = None
        tracklist.match_confidence = None
        tracklist.auto_linked = False
        await session.commit()

    return await _render_tracklist_list(request, session, "all")


@router.post("/{tracklist_id}/rescrape", response_class=HTMLResponse)
async def rescrape_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Enqueue a re-scrape job for a tracklist."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        routed = await resolve_queue_for_task("scrape_and_store_tracklist", request.app.state, session)
        await routed.queue.enqueue("scrape_and_store_tracklist", tracklist_id=str(tracklist_id))

    has_candidates = await _has_candidates(session, tracklist) if tracklist else False
    if tracklist:
        await _attach_track_count(session, tracklist)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/tracklist_card.html",
        context={"request": request, "tracklist": tracklist, "rescrape_queued": True, "cue_version": 0, "has_candidates": has_candidates},
    )


@router.get("/{tracklist_id}/search", response_class=HTMLResponse)
async def search_better_match(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search for a better match for an existing tracklist."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()

    search_results: list[dict[str, Any]] = []
    query = ""

    # phaze-ctrl: this is the only in-request live-scrape path (rescrape/manual_search enqueue to
    # SAQ). scraper.search() first sleeps 2-5s on the rate limiter, then POSTs with a 30s timeout, so
    # the handler would otherwise pin a PgBouncer SESSION-mode pooled connection idle-in-transaction
    # for ~2-35s of pure network I/O -- a handful of concurrent clicks (or a slow/unreachable upstream)
    # drains the capped pool and 500s /health + normal page loads. So do ALL the DB reads up front,
    # capture the primitives the scrape + render need into locals, then RELEASE the connection
    # (session.commit ends the implicit read transaction and returns the connection to the pool)
    # BEFORE any network I/O. No DB write happens after the scrape.
    file_id: uuid.UUID | None = tracklist.file_id if tracklist else None
    file_artist: str | None = None
    file_event: str | None = None
    file_date: Any = None
    if tracklist:
        parts = []
        if tracklist.artist:
            parts.append(tracklist.artist)
        if tracklist.event:
            parts.append(tracklist.event)
        query = " ".join(parts) if parts else ""

        if query:
            # Score candidates against the FILE this tracklist is (or would be) linked to, not
            # against the tracklist's own artist/event/date (phaze-ldal) -- that comparison
            # degenerates into a same-artist self-match that reads near-100 for ANY result.
            file_artist, file_event, file_date = await _file_match_context(session, file_id)

    # Release the pooled connection before the rate-limit sleep + HTTP scrape (phaze-ctrl).
    await session.commit()

    if query:
        scraper = TracklistScraper()
        try:
            raw_results = await scraper.search(query)
            for r in raw_results:
                conf = compute_match_confidence(
                    tracklist_artist=r.artist,
                    tracklist_event=None,
                    tracklist_date=None,
                    file_artist=file_artist,
                    file_event=file_event,
                    file_date=file_date,
                )
                search_results.append(
                    {
                        "external_id": r.external_id,
                        "title": r.title,
                        "url": r.url,
                        "artist": r.artist,
                        "confidence": conf,
                    }
                )
            search_results.sort(key=lambda x: x["confidence"], reverse=True)
        finally:
            await scraper.close()

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/search_results.html",
        context={"request": request, "results": search_results, "query": query, "file_id": file_id},
    )


@router.post("/link-result", response_class=HTMLResponse)
async def link_search_result(
    request: Request,
    file_id: uuid.UUID = Form(...),
    external_id: str = Form(...),
    url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Link a selected 'Find Better Match' search RESULT to a file.

    Fixes phaze-ldal: the old ``/{tracklist_id}/link`` route trusted a path param that was
    the same for every result on the panel (the tracklist being searched FROM), so picking any
    result silently re-linked that ORIGINAL tracklist and overwrote its confidence with the
    selected result's score -- the result's own content (external_id/url/tracks) was never
    fetched or persisted. Here the selected result's own identity resolves (or scrapes+stores)
    its OWN Tracklist row, which is what gets linked and honestly scored -- the original
    tracklist this search started from is never touched.
    """
    if not _is_allowed_tracklist_url(url):
        return HTMLResponse(content="Invalid tracklist url", status_code=400)

    # phaze-gkow: scrape_tracklist() first sleeps 2-5s on the rate limiter then POSTs with a 30s
    # timeout. Holding the injected session's implicit transaction (a pinned PgBouncer SESSION-mode
    # pooled connection) across that ~2-35s of network I/O drains the capped pool. So do the reads in
    # a short transaction, RELEASE the connection before the scrape, then re-open a short transaction
    # to store + link. Structure: (1) validate file + probe whether we already have this external_id,
    # commit; (2) scrape with NO connection held; (3) store the scrape in its OWN transaction so a
    # later stale-file 404 never discards it; (4) re-validate the file and link.
    #
    # phaze-x4vi (preserved): validate the client-supplied file_id BEFORE the expensive network scrape.
    # file_id is an FK to files.id; a stale/forged id -- e.g. the FileRecord deleted by a concurrent
    # scan while this panel stayed open -- would otherwise sail through to the final commit and detonate
    # as a ForeignKeyViolation -> 500. Resolving the file up front returns a clean error and skips the
    # wasted scrape entirely.
    if await session.get(FileRecord, file_id) is None:
        return HTMLResponse(content="File not found", status_code=404)

    result = await session.execute(select(Tracklist).where(Tracklist.external_id == external_id))
    needs_scrape = result.scalar_one_or_none() is None
    # Release the pooled connection before the rate-limit sleep + HTTP scrape (phaze-gkow).
    await session.commit()

    scraped = None
    if needs_scrape:
        scraper = TracklistScraper()
        try:
            scraped = await scraper.scrape_tracklist(url)
        finally:
            await scraper.close()

    if scraped is not None:
        # Store the scrape in its own transaction and commit it FIRST -- so if the file was deleted
        # DURING the scrape, the stale-file 404 below never discards the freshly scraped tracklist
        # (phaze-gkow preserves the phaze-x4vi "no scraped work discarded" property). _store_scraped_tracklist
        # is idempotent under a per-external_id advisory lock, so a sibling job that stored the same
        # external_id mid-scrape is folded in rather than duplicated.
        selected = await _store_scraped_tracklist(session, scraped)
        await session.commit()
    else:
        result = await session.execute(select(Tracklist).where(Tracklist.external_id == external_id))
        selected = result.scalar_one_or_none()
        if selected is None:
            # The row we saw before releasing the connection was deleted in the interim; nothing to link.
            return HTMLResponse(content="Tracklist not found", status_code=404)

    # phaze-gkow: re-validate the file in the (re-opened) linkage transaction -- it may have been deleted
    # during the scrape. Returning 404 here leaves any freshly scraped tracklist persisted (unlinked),
    # never discarded, and avoids a ForeignKeyViolation -> 500 on the final commit.
    if await session.get(FileRecord, file_id) is None:
        return HTMLResponse(content="File not found", status_code=404)

    file_artist, file_event, file_date = await _file_match_context(session, file_id)
    confidence = compute_match_confidence(
        tracklist_artist=selected.artist,
        tracklist_event=selected.event,
        tracklist_date=selected.date,
        file_artist=file_artist,
        file_event=file_event,
        file_date=file_date,
    )

    selected.file_id = file_id
    selected.match_confidence = confidence
    selected.auto_linked = False
    await session.commit()

    return await _render_tracklist_list(request, session, "all")


@router.post("/search", response_class=HTMLResponse)
async def manual_search(
    request: Request,
    file_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Manual search for an unmatched file."""
    routed = await resolve_queue_for_task("search_tracklist", request.app.state, session)
    await routed.queue.enqueue("search_tracklist", file_id=str(file_id))

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/search_results.html",
        context={"request": request, "results": [], "query": "", "file_id": file_id, "loading": True},
    )


@router.post("/{tracklist_id}/undo-link", response_class=HTMLResponse)
async def undo_link(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Undo an auto-link (per D-14, D-23)."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if tracklist:
        tracklist.file_id = None
        tracklist.match_confidence = None
        tracklist.auto_linked = False
        await session.commit()

    return await _render_tracklist_list(request, session, "all")


@router.get("/tracks/{track_id}/edit/{field}", response_class=HTMLResponse)
async def edit_track_field(
    request: Request,
    track_id: uuid.UUID,
    field: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return inline edit input for a track field."""
    if field not in EDITABLE_FIELDS:
        return HTMLResponse(content="Invalid field", status_code=400)

    result = await session.execute(select(TracklistTrack).where(TracklistTrack.id == track_id))
    track = result.scalar_one_or_none()
    if not track:
        return HTMLResponse(content="Track not found", status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/inline_edit_field.html",
        context={"request": request, "track": track, "field": field},
    )


@router.put("/tracks/{track_id}/edit/{field}", response_class=HTMLResponse)
async def save_track_field(
    request: Request,
    track_id: uuid.UUID,
    field: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save an inline edit for a track field and return display-mode HTML."""
    if field not in EDITABLE_FIELDS:
        return HTMLResponse(content="Invalid field", status_code=400)

    result = await session.execute(select(TracklistTrack).where(TracklistTrack.id == track_id))
    track = result.scalar_one_or_none()
    if not track:
        return HTMLResponse(content="Track not found", status_code=404)

    form_data = await request.form()
    new_value = str(form_data.get(field, ""))

    # wire_bounds rule 1/4: reject an over-width value AT THE BOUNDARY, before setattr/commit ever
    # opens a transaction against ``timestamp``'s String(20) column -- not by catching the Postgres
    # StringDataRightTruncation a commit would raise (phaze-81au). This route parses its own form
    # body (one PUT serves three fields with three different bounds) rather than a declared FastAPI
    # ``Form(...)`` param, so the guard is manual here instead of a Field/Form constraint.
    #
    # This is an OPERATOR-facing inline edit, not the agent wire path: a bare 422 that blanks what
    # they just typed is a worse outcome than the 500 it replaces. So the rejection re-renders the
    # SAME edit input, preserving their typed value verbatim plus an inline reason, instead of
    # discarding it -- the front end opts this response back into the swap despite the 4xx status
    # (the ``htmx:beforeSwap`` handler in shell.html, same pattern as the WR-01 404 opt-in).
    max_length = EDITABLE_FIELD_MAX_LENGTHS.get(field)
    if max_length is not None and len(new_value) > max_length:
        return templates.TemplateResponse(
            request=request,
            name="tracklists/partials/inline_edit_field.html",
            context={
                "request": request,
                "track": track,
                "field": field,
                "value": new_value,
                "error": f"{field} must be {max_length} characters or fewer (got {len(new_value)}).",
            },
            status_code=422,
        )

    # phaze-jsl9: normalize a cleared/whitespace-only edit to NULL rather than persisting "" --
    # every CUE eligibility predicate (routers/cue.py) keys on ``timestamp.is_not(None)``, so a ""
    # value silently kept a cleared track "eligible" while parse_timestamp_string('') raised. This
    # also keeps the write side consistent with the display side, which already coalesces a falsy
    # value to "-" (below).
    stripped_value = new_value.strip()

    # For timestamp specifically, validate against the accepted HH:MM:SS / MM:SS / float-seconds
    # grammar (the same grammar parse_timestamp_string implements) BEFORE it ever reaches the
    # column -- this router is the only path that can write a non-NULL, non-parseable value into
    # TracklistTrack.timestamp (the agent wire path only enforces length). Reject at the boundary
    # like the over-length branch above: 422, same edit input, value preserved, inline reason.
    if field == "timestamp" and stripped_value:
        try:
            parsed_timestamp = parse_timestamp_string(stripped_value)
        except ValueError:
            parsed_timestamp = None
        if parsed_timestamp is None:
            return templates.TemplateResponse(
                request=request,
                name="tracklists/partials/inline_edit_field.html",
                context={
                    "request": request,
                    "track": track,
                    "field": field,
                    "value": new_value,
                    "error": f"timestamp must be HH:MM:SS, MM:SS, or seconds (e.g. '90.5') -- could not parse {stripped_value!r}.",
                },
                status_code=422,
            )

    setattr(track, field, stripped_value or None)
    await session.commit()
    await session.refresh(track)

    # Return display-mode HTML via Jinja2 template (auto-escaped, remains clickable)
    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/inline_display_field.html",
        context={"request": request, "track": track, "field": field, "value": getattr(track, field) or "-"},
    )


@router.delete("/tracks/{track_id}", response_class=HTMLResponse)
async def delete_track(
    track_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Delete a track from a tracklist. Returns empty string for outerHTML swap."""
    result = await session.execute(select(TracklistTrack).where(TracklistTrack.id == track_id))
    track = result.scalar_one_or_none()
    if not track:
        return HTMLResponse(content="Track not found", status_code=404)

    # fk_discogs_links_track_id_tracklist_tracks carries NO ON DELETE and there is no ORM cascade
    # (DiscogsLink.track is one-directional lazy='noload', TracklistTrack has no inverse), so a matched
    # track keeps referencing DiscogsLink rows -- deleting it directly raises IntegrityError (500).
    # Clear the referencing links first, mirroring scan_deletion.py's child->parent ordering.
    await session.execute(delete(DiscogsLink).where(DiscogsLink.track_id == track_id))
    await session.delete(track)
    await session.commit()
    return HTMLResponse(content="")


@router.post("/{tracklist_id}/approve", response_class=HTMLResponse)
async def approve_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Approve a tracklist (set status to 'approved')."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if not tracklist:
        return HTMLResponse(content="Tracklist not found", status_code=404)

    tracklist.status = "approved"
    await session.commit()

    cue_version = 0
    if tracklist.file_id:
        fr_result = await session.execute(select(FileRecord).where(FileRecord.id == tracklist.file_id))
        fr = fr_result.scalar_one_or_none()
        if fr and await is_applied(session, fr.id):
            cue_version = _get_cue_version(fr.current_path)

    await _attach_track_count(session, tracklist)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/tracklist_card.html",
        context={"request": request, "tracklist": tracklist, "cue_version": cue_version, "has_candidates": await _has_candidates(session, tracklist)},
    )


@router.post("/{tracklist_id}/reject", response_class=HTMLResponse)
async def reject_tracklist(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Reject a tracklist (set status to 'rejected')."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if not tracklist:
        return HTMLResponse(content="Tracklist not found", status_code=404)

    tracklist.status = "rejected"
    await session.commit()

    await _attach_track_count(session, tracklist)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/tracklist_card.html",
        context={"request": request, "tracklist": tracklist, "cue_version": 0, "has_candidates": False},
    )


@router.post("/{tracklist_id}/reject-low", response_class=HTMLResponse)
async def reject_low_confidence(
    request: Request,
    tracklist_id: uuid.UUID,
    threshold: int = Form(50, ge=0, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Bulk reject tracks below a confidence threshold."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if not tracklist:
        return HTMLResponse(content="Tracklist not found", status_code=404)

    if tracklist.latest_version_id:
        # The set of low-confidence tracks about to be removed from the latest version.
        low_conf_tracks = select(TracklistTrack.id).where(
            TracklistTrack.version_id == tracklist.latest_version_id,
            TracklistTrack.confidence.is_not(None),
            TracklistTrack.confidence < threshold,
        )
        # Clear referencing DiscogsLink rows FIRST (fk_discogs_links_track_id has no ON DELETE and Core
        # bulk delete never fires ORM cascades). One matched low-confidence track would otherwise raise
        # IntegrityError and roll back this single-statement transaction -- making reject-low wholly
        # inoperable on any matched tracklist.
        await session.execute(delete(DiscogsLink).where(DiscogsLink.track_id.in_(low_conf_tracks)))
        # Delete low-confidence tracks from the latest version
        await session.execute(
            delete(TracklistTrack).where(
                TracklistTrack.version_id == tracklist.latest_version_id,
                TracklistTrack.confidence.is_not(None),
                TracklistTrack.confidence < threshold,
            )
        )
        await session.commit()

        # Reload remaining tracks
        version_result = await session.execute(
            select(TracklistVersion).options(selectinload(TracklistVersion.tracks)).where(TracklistVersion.id == tracklist.latest_version_id)
        )
        version = version_result.scalar_one_or_none()
        tracks = sorted(version.tracks, key=lambda t: t.position) if version else []
    else:
        tracks = []

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/fingerprint_track_detail.html",
        context={"request": request, "tracklist": tracklist, "tracks": tracks},
    )


@router.post("/{tracklist_id}/match-discogs", response_class=HTMLResponse)
async def match_discogs(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Enqueue a SAQ task to match all tracks in this tracklist to Discogs releases."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if not tracklist:
        return HTMLResponse(content="Tracklist not found", status_code=404)

    routed = await resolve_queue_for_task("match_tracklist_to_discogs", request.app.state, session)
    await routed.queue.enqueue("match_tracklist_to_discogs", tracklist_id=str(tracklist_id))

    await _attach_track_count(session, tracklist)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/tracklist_card.html",
        context={
            "request": request,
            "tracklist": tracklist,
            "match_queued": True,
            "cue_version": 0,
            "has_candidates": await _has_candidates(session, tracklist),
        },
    )


@router.get("/{tracklist_id}/tracks/{track_id}/discogs", response_class=HTMLResponse)
async def get_discogs_candidates(
    request: Request,
    tracklist_id: uuid.UUID,  # noqa: ARG001
    track_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return Discogs candidate rows for a single track."""
    stmt = select(DiscogsLink).where(DiscogsLink.track_id == track_id, DiscogsLink.status != "dismissed").order_by(DiscogsLink.confidence.desc())
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/discogs_candidates.html",
        context={"request": request, "track_id": str(track_id), "candidates": candidates},
    )


@router.post("/discogs-links/{link_id}/accept", response_class=HTMLResponse)
async def accept_discogs_link(
    request: Request,
    link_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Accept a Discogs candidate link and auto-dismiss siblings for the same track."""
    result = await session.execute(select(DiscogsLink).where(DiscogsLink.id == link_id))
    link = result.scalar_one_or_none()
    if not link:
        return HTMLResponse(content="Link not found", status_code=404)

    track_id = link.track_id
    try:
        # Dismiss all other links for the same track FIRST -- status-blind, so a pre-existing
        # accepted link for this track (e.g. from a prior accept) is dismissed too, not just
        # candidates. This must happen (and flush) before we set this link to "accepted" below,
        # or the two writes could transiently coexist as two accepted rows for the same track
        # and trip the one-accepted-per-track partial unique index (D-07).
        siblings_stmt = select(DiscogsLink).where(
            DiscogsLink.track_id == link.track_id,
            DiscogsLink.id != link.id,
        )
        siblings_result = await session.execute(siblings_stmt)
        for sibling in siblings_result.scalars().all():
            sibling.status = "dismissed"
        await session.flush()

        # Accept this link
        link.status = "accepted"

        await session.commit()
    except StaleDataError:
        # phaze-xdu1: match_tracklist_to_discogs's short candidate-swap transaction committed between
        # our SELECT and this flush -- deleting a row we were about to update, so the ORM UPDATE matched
        # 0 rows. Roll back and re-render the CURRENT (freshly matched) candidates instead of 500ing and
        # silently losing the operator's click; the panel refreshes to the new candidate set.
        await session.rollback()
        return await _render_discogs_candidates(request, session, track_id, status_code=409)

    # Re-query remaining candidates (only the accepted one should remain visible)
    return await _render_discogs_candidates(request, session, track_id)


@router.delete("/discogs-links/{link_id}", response_class=HTMLResponse)
async def dismiss_discogs_link(
    request: Request,
    link_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Dismiss a Discogs candidate link."""
    result = await session.execute(select(DiscogsLink).where(DiscogsLink.id == link_id))
    link = result.scalar_one_or_none()
    if not link:
        return HTMLResponse(content="Link not found", status_code=404)

    track_id = link.track_id
    try:
        link.status = "dismissed"
        await session.commit()
    except StaleDataError:
        # phaze-xdu1: the concurrent match task deleted this candidate between our SELECT and flush.
        # Roll back and re-render the current candidates rather than 500ing -- the row the operator
        # dismissed is already gone, so the effect they wanted (it's not a candidate) already holds.
        await session.rollback()
        return await _render_discogs_candidates(request, session, track_id, status_code=409)

    # Re-query remaining candidates
    return await _render_discogs_candidates(request, session, track_id)


@router.post("/{tracklist_id}/bulk-link", response_class=HTMLResponse)
async def bulk_link_discogs(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Bulk-link all tracks to their highest-confidence Discogs candidate."""
    result = await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))
    tracklist = result.scalar_one_or_none()
    if not tracklist:
        return HTMLResponse(content="Tracklist not found", status_code=404)

    if not tracklist.latest_version_id:
        return templates.TemplateResponse(
            request=request,
            name="tracklists/partials/track_detail.html",
            context={"request": request, "tracks": []},
        )

    # Load all tracks for this tracklist version
    tracks_result = await session.execute(select(TracklistTrack).where(TracklistTrack.version_id == tracklist.latest_version_id))
    tracks = list(tracks_result.scalars().all())
    track_ids = [t.id for t in tracks]

    if track_ids:
        # Load all non-dismissed links for these tracks -- status-blind, mirroring
        # accept_discogs_link's sibling dismissal -- so a track that already carries an
        # accepted link (e.g. from a prior individual accept, still present because
        # match_tracklist_to_discogs only deletes candidate-status rows) is folded into
        # this same accept/dismiss pass instead of being left as a second accepted row
        # once a fresh candidate is promoted (D-07).
        candidates_stmt = select(DiscogsLink).where(
            DiscogsLink.track_id.in_(track_ids),
            DiscogsLink.status != "dismissed",
        )
        candidates_result = await session.execute(candidates_stmt)
        all_candidates = list(candidates_result.scalars().all())

        # Group by track_id, find highest confidence per track
        by_track: dict[uuid.UUID, list[DiscogsLink]] = defaultdict(list)
        for c in all_candidates:
            by_track[c.track_id].append(c)

        tops: list[DiscogsLink] = []
        for _tid, track_candidates in by_track.items():
            # Sort by confidence descending; the top link wins (it may already be the
            # accepted one, in which case this is a no-op for that track).
            track_candidates.sort(key=lambda x: x.confidence, reverse=True)
            top = track_candidates[0]
            tops.append(top)
            # Dismiss the rest now, BEFORE accepting any top link below. Doing the accept
            # first (or interleaved) could transiently leave two accepted rows for the same
            # track and trip the one-accepted-per-track partial unique index (D-07).
            for other in track_candidates[1:]:
                other.status = "dismissed"

        try:
            await session.flush()

            for top in tops:
                top.status = "accepted"

            await session.commit()
        except StaleDataError:
            # phaze-xdu1: match_tracklist_to_discogs's short candidate-swap transaction committed
            # between the candidate SELECT above and this flush, deleting rows we were about to update.
            # Roll back and fall through to re-render the current track state instead of 500ing -- the
            # operator can re-run bulk-link against the freshly matched candidates.
            await session.rollback()

    # Reload tracks for display
    version_result = await session.execute(
        select(TracklistVersion).options(selectinload(TracklistVersion.tracks)).where(TracklistVersion.id == tracklist.latest_version_id)
    )
    version = version_result.scalar_one_or_none()
    display_tracks = sorted(version.tracks, key=lambda t: t.position) if version else []

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/track_detail.html",
        context={"request": request, "tracks": display_tracks, "tracklist_id": tracklist_id},
    )


async def _render_tracklist_list(request: Request, session: AsyncSession, filter_value: str, page: int = 1, page_size: int = 20) -> HTMLResponse:
    """Render the tracklist list partial with stats."""
    stmt = select(Tracklist)
    if filter_value == "matched":
        stmt = stmt.where(Tracklist.file_id.is_not(None))
    elif filter_value == "unmatched":
        stmt = stmt.where(Tracklist.file_id.is_(None))
    elif filter_value == "proposed":
        stmt = stmt.where(Tracklist.status == "proposed")

    # Same unique-tiebreaker rationale as ``list_tracklists`` above (phaze-rgxg): ``match_confidence``
    # + ``created_at`` are both non-unique, so appending ``Tracklist.id`` DESC keeps the OFFSET/LIMIT
    # boundary this helper renders (always page 1 today -- every caller uses the default) consistent
    # with the page-2+ boundary ``list_tracklists`` computes from the SAME ORDER BY.
    stmt = stmt.order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.created_at.desc(), Tracklist.id.desc())

    total = await _get_tracklist_count(session, filter_value)
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)

    result = await session.execute(stmt)
    tracklists = list(result.scalars().all())

    for tl in tracklists:
        if tl.latest_version_id:
            count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tl.latest_version_id))
            tl._track_count = count_result.scalar() or 0  # type: ignore[attr-defined]
        else:
            tl._track_count = 0  # type: ignore[attr-defined]

    # Compute has_candidates for each tracklist
    for tl in tracklists:
        if tl.latest_version_id:
            track_ids_stmt = select(TracklistTrack.id).where(TracklistTrack.version_id == tl.latest_version_id)
            cand_result = await session.execute(
                select(func.count(DiscogsLink.id)).where(
                    DiscogsLink.track_id.in_(track_ids_stmt),
                    DiscogsLink.status == "candidate",
                )
            )
            tl._has_candidates = (cand_result.scalar() or 0) > 0  # type: ignore[attr-defined]
        else:
            tl._has_candidates = False  # type: ignore[attr-defined]

    # Compute CUE version for approved tracklists with executed files
    for tl in tracklists:
        if tl.status == "approved" and tl.file_id:
            fr_result = await session.execute(select(FileRecord).where(FileRecord.id == tl.file_id))
            fr = fr_result.scalar_one_or_none()
            if fr and await is_applied(session, fr.id):
                tl._cue_version = _get_cue_version(fr.current_path)  # type: ignore[attr-defined]
            else:
                tl._cue_version = 0  # type: ignore[attr-defined]
        else:
            tl._cue_version = 0  # type: ignore[attr-defined]

    stats = await _get_tracklist_stats(session)
    pagination = Pagination(page=page, page_size=page_size, total=total)

    return templates.TemplateResponse(
        request=request,
        name="tracklists/partials/tracklist_list.html",
        context={
            "request": request,
            "tracklists": tracklists,
            "stats": stats,
            "pagination": pagination,
            "current_page": "tracklists",
            "active_filter": filter_value,
            # phaze-64uy: the wrapper gate comes from the handler, never from the raw header
            # inside the template. Every caller of this helper is an htmx POST landing in the
            # existing ``#tracklists-list``, so a live swap suppresses the duplicate wrapper and
            # any other shape (never a restore -- these are POSTs) emits exactly one.
            "is_hx": wants_fragment(request),
        },
    )
