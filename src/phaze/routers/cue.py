"""CUE sheet management UI router -- generation, batch generation, and CUE management page."""

import asyncio
from pathlib import Path
import re
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers.response_shape import wants_fragment
from phaze.services.cue_generator import CueTrackData, generate_cue_content, parse_timestamp_string, write_cue_file
from phaze.services.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MIN_PAGE_SIZE, Page, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.stage_status import applied_clause, is_applied


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/cue", tags=["cue"])


# The CUE list's display order (phaze-hdho): fingerprint-sourced tracklists first (D-02 / CUE-01
# preference), then alphabetically by artist/event. BOTH ``Tracklist.artist`` and ``Tracklist.event``
# are nullable ``Text`` (models/tracklist.py), so a set of tracklists sharing a source and the same
# -- or NULL -- artist/event forms a tie group. This tuple alone is NOT a unique sort key; every
# caller MUST append a ``Tracklist.id`` tiebreaker (directly, or via :func:`paged_stmt`) before
# relying on it for anything that slices or pages the result set (paging contract rule 4).
_ELIGIBLE_DISPLAY_ORDER: tuple[Any, ...] = (
    (Tracklist.source == "fingerprint").desc(),
    Tracklist.artist,
    Tracklist.event,
)


def _eligible_tracklist_stmt() -> Select[Any]:
    """Build the base (UNORDERED) SELECT for approved tracklists with EXECUTED files that have >=1 timestamped track.

    Shared by every eligible-tracklist reader so the join/filter logic lives in exactly one place.
    Deliberately carries NO ``ORDER BY`` -- callers compose :data:`_ELIGIBLE_DISPLAY_ORDER` (+ the
    mandatory ``Tracklist.id`` tiebreaker) themselves, so it is applied exactly once per statement
    instead of risking a double-appended sort key.

    phaze-dboy: the timestamp-existence check is scoped to ``Tracklist.latest_version_id`` --
    NOT "any version ever" -- because that is the ONLY version actual generation ever reads
    (``_build_cue_tracks(session, tracklist.latest_version_id)`` in both ``generate_cue`` and
    ``generate_batch``). A re-scrape/re-fingerprint can create a newer ``latest_version_id``
    whose tracks carry no timestamps while an OLDER version still has them; scoping this
    predicate to "any version" previously listed that tracklist as eligible with a Generate
    button that could never succeed (always "No tracks have timestamps"), permanently
    inflating ``eligible`` past ``generated`` with no way to converge.
    """
    # Subquery: TracklistVersion ids that have at least one track with a timestamp. Matched
    # against ``Tracklist.latest_version_id`` below (NOT ``Tracklist.id`` via ``TracklistVersion.
    # tracklist_id``) so this evaluates the SAME version generation reads -- see phaze-dboy above.
    has_timestamp_subq = select(TracklistTrack.version_id).where(TracklistTrack.timestamp.is_not(None)).distinct()

    return (
        select(Tracklist, FileRecord)
        .join(FileRecord, Tracklist.file_id == FileRecord.id)
        .where(
            Tracklist.status == "approved",
            Tracklist.file_id.is_not(None),
            applied_clause(),
            Tracklist.latest_version_id.in_(has_timestamp_subq),
        )
    )


async def _get_eligible_tracklist_query(session: AsyncSession, *, limit: int | None = None) -> list[tuple[Tracklist, FileRecord]]:
    """Query approved tracklists with EXECUTED files that have at least one timestamped track.

    Ordered by :data:`_ELIGIBLE_DISPLAY_ORDER` with ``Tracklist.id`` appended as a tiebreaker so a
    caller-supplied ``limit`` (a bare SQL ``LIMIT``, no ``OFFSET``) is deterministic even when many
    rows tie on source/artist/event -- WITHOUT it, WHICH rows fall inside the cap could vary between
    executions (phaze-hdho).

    Pass ``limit`` to bound the result set at the SQL level. WR-03: the review-card consumer
    (``services.review.get_cue_review_cards``) passes ``limit=_MAX_REVIEW_ROWS`` so the DB never
    returns more than the render cap -- the eligible half is then genuinely memory-bounded, not just
    loop-capped after materializing every eligible pair. The count/batch callers (``_get_cue_stats``,
    ``generate_batch``) call with no ``limit`` and get the full set. The OFFSET-paged render read in
    ``list_cue`` does NOT use this helper -- see :func:`paged_stmt` there instead.
    """
    stmt = _eligible_tracklist_stmt().order_by(*_ELIGIBLE_DISPLAY_ORDER, Tracklist.id)
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    return list(result.tuples().all())


def _count_generated_sync(pairs: list[tuple[Tracklist, FileRecord]]) -> int:
    """Synchronous filesystem probe for the 'generated' stat (phaze-rkvb).

    Bundles the per-file :func:`_get_cue_version` probe (``Path.exists`` plus, when a base ``.cue``
    exists, a full ``iterdir`` of the audio file's parent directory) for the WHOLE eligible set into
    ONE function, run via :func:`asyncio.to_thread` from :func:`_get_cue_stats`. The eligible set
    carries no ``LIMIT`` and the media files live on the documented NFS/SMB file-server mount, so
    looping this synchronously on the event loop -- as it did before this fix -- blocked every SSE
    stream, poll, and concurrent request for the scan's duration, with no timeout if the mount
    stalls (an unbounded, unrecoverable freeze of the single API worker).
    """
    return sum(1 for _tl, fr in pairs if _get_cue_version(fr.current_path) > 0)


async def _missing_timestamps_count(session: AsyncSession) -> int:
    """Count approved + EXECUTED-file tracklists with NO tracks with timestamps on the LATEST
    version (phaze-dboy -- mirrors ``_eligible_tracklist_stmt``'s scope so this is the true
    inverse of "eligible", not a broader "any version" set that undercounts).

    A pure ``SELECT count(...)`` -- NOT a full-corpus materialization -- so callers that already
    hold the eligible set in memory (e.g. :func:`generate_batch`) can call this directly instead
    of going through :func:`_get_cue_stats` and re-querying the eligible set a second time.
    """
    has_timestamp_subq = select(TracklistTrack.version_id).where(TracklistTrack.timestamp.is_not(None)).distinct()

    missing_stmt = (
        select(func.count(Tracklist.id))
        .join(FileRecord, Tracklist.file_id == FileRecord.id)
        .where(
            Tracklist.status == "approved",
            Tracklist.file_id.is_not(None),
            applied_clause(),
            # A tracklist with no version at all (latest_version_id IS NULL) has no timestamps
            # either -- NULL.not_in(...) is SQL NULL (neither true nor false), so it must be
            # OR'd in explicitly or it silently drops out of the "missing" count.
            or_(Tracklist.latest_version_id.is_(None), Tracklist.latest_version_id.not_in(has_timestamp_subq)),
        )
    )
    missing_result = await session.execute(missing_stmt)
    return missing_result.scalar() or 0


async def _cue_stats_from_eligible_pairs(session: AsyncSession, eligible_pairs: list[tuple[Tracklist, FileRecord]]) -> dict[str, int]:
    """Compute CUE generation statistics from an ALREADY-materialized eligible set (phaze-8lpg).

    Shared by :func:`_get_cue_stats` (which materializes the eligible set itself) and
    :func:`generate_batch` (which already holds it from its generation loop) so a caller that has
    the eligible set in hand never re-queries it just to compute stats.
    """
    generated = await asyncio.to_thread(_count_generated_sync, eligible_pairs)
    missing_timestamps = await _missing_timestamps_count(session)
    return {"eligible": len(eligible_pairs), "generated": generated, "missing_timestamps": missing_timestamps}


async def _get_cue_stats(session: AsyncSession) -> dict[str, int]:
    """Compute CUE generation statistics."""
    # Eligible: approved + EXECUTED file + has timestamps
    eligible_pairs = await _get_eligible_tracklist_query(session)
    return await _cue_stats_from_eligible_pairs(session, eligible_pairs)


def _get_cue_version(file_path: str) -> int:
    """Check filesystem for existing CUE files and return the version number.

    Returns 0 if no CUE exists, 1 if base.cue exists, N for highest .vN.cue.
    """
    audio_path = Path(file_path)
    base_cue = audio_path.parent / f"{audio_path.stem}.cue"

    if not base_cue.exists():
        return 0

    max_version = 1
    pattern = re.compile(rf"^{re.escape(audio_path.stem)}\.v(\d+)\.cue$")
    for f in audio_path.parent.iterdir():
        m = pattern.match(f.name)
        if m:
            max_version = max(max_version, int(m.group(1)))

    return max_version


async def _build_cue_tracks(
    session: AsyncSession,
    version_id: uuid.UUID,
) -> list[CueTrackData]:
    """Build CueTrackData list from a tracklist version's tracks + Discogs links."""
    # Load tracks
    version_result = await session.execute(
        select(TracklistVersion).options(selectinload(TracklistVersion.tracks)).where(TracklistVersion.id == version_id)
    )
    version = version_result.scalar_one_or_none()
    if not version:
        return []

    tracks = sorted(version.tracks, key=lambda t: t.position)
    track_ids = [t.id for t in tracks]

    # Load accepted Discogs links for these tracks
    discogs_by_track: dict[uuid.UUID, DiscogsLink] = {}
    if track_ids:
        discogs_stmt = select(DiscogsLink).where(
            DiscogsLink.track_id.in_(track_ids),
            DiscogsLink.status == "accepted",
        )
        discogs_result = await session.execute(discogs_stmt)
        for link in discogs_result.scalars().all():
            discogs_by_track[link.track_id] = link

    cue_tracks: list[CueTrackData] = []
    for track in tracks:
        ts = parse_timestamp_string(track.timestamp)
        discogs_link = discogs_by_track.get(track.id)
        cue_tracks.append(
            CueTrackData(
                position=track.position,
                title=track.title,
                artist=track.artist,
                timestamp_seconds=ts,
                genre=None,  # DiscogsLink has no genre field (D-09)
                label=discogs_link.discogs_label if discogs_link else None,
                year=discogs_link.discogs_year if discogs_link else None,
            )
        )

    return cue_tracks


async def _load_tracklist_with_file(session: AsyncSession, tracklist_id: uuid.UUID) -> tuple[Tracklist | None, FileRecord | None]:
    """Load tracklist joined with file record."""
    stmt = select(Tracklist, FileRecord).join(FileRecord, Tracklist.file_id == FileRecord.id, isouter=True).where(Tracklist.id == tracklist_id)
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None, None
    return row[0], row[1]


@router.get("/", response_class=HTMLResponse)
async def list_cue(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=MIN_PAGE_SIZE, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render ONE bounded page of the CUE management list.

    phaze-hdho: this used to re-run the UNORDERED-past-its-display-key eligible query on every page
    request and slice the fully materialized list in Python (``eligible_pairs[offset:offset+page_size]``).
    ``Tracklist.artist``/``Tracklist.event`` are both nullable, so any set of tracklists sharing a
    source and the same (or NULL) artist/event tied on the ORDER BY -- and Postgres gives NO stability
    guarantee for tied rows across two SEPARATE query executions. Page 1 and page 2 are independent
    HTTP requests, each re-running the query, so a boundary row's tie-group placement could flip
    between them: silently duplicated onto both pages, or dropped from both.

    Routed through :mod:`phaze.services.pagination` instead: :func:`paged_stmt` appends the mandatory
    unique ``Tracklist.id`` tiebreaker AFTER the existing display order (contract rule 4), so the
    total ordering is deterministic and OFFSET paging can no longer skip or duplicate a row. ``page``/
    ``page_size`` clamp rather than raise (rule 5); ``has_next`` rides the ``page_size + 1`` sentinel,
    never a whole-corpus COUNT (rule 2); the read is SAVEPOINT degrade-safe (rule 6) -- any DB error
    rolls back the nested scope alone and renders an EMPTY page rather than 500ing the workspace.
    """
    # SHELL-05 (D-03): a plain (non-HX) GET / bookmark resolves into the v7.0 shell.
    # The in-page HX filter branch below is left intact so the app stays usable (D-01).
    #
    # phaze-64uy (HYGIENE, not a live defect): ``wants_fragment`` per response_shape.py contract
    # rule 1. No template pushes a ``/cue/`` URL, so no history restore can reach this handler
    # today; the conversion removes the banned raw-header branch and makes the handler correct in
    # advance of anything adding ``hx-push-url`` to the cue controls.
    if not wants_fragment(request):
        return RedirectResponse(url="/s/cue", status_code=302)

    stats = await _get_cue_stats(session)

    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    try:
        async with session.begin_nested():
            stmt = paged_stmt(
                _eligible_tracklist_stmt(),
                page=page,
                page_size=page_size,
                order_by=_ELIGIBLE_DISPLAY_ORDER,
                tiebreaker=(Tracklist.id,),
            )
            raw = (await session.execute(stmt)).tuples().all()
    except Exception:
        logger.warning("cue_list_page_degraded", page=page, page_size=page_size, exc_info=True)
        raw = []
    page_pairs, has_next = split_sentinel(list(raw), page_size)

    # Build tracklist data with CUE status
    tracklists: list[dict[str, Any]] = []
    for tl, fr in page_pairs:
        cue_version = _get_cue_version(fr.current_path)

        # Load track count
        if tl.latest_version_id:
            count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tl.latest_version_id))
            track_count = count_result.scalar() or 0
        else:
            track_count = 0

        tracklists.append(
            {
                "id": tl.id,
                "artist": tl.artist or "Unknown Artist",
                "event": tl.event or "",
                "date": tl.date,
                "track_count": track_count,
                "cue_version": cue_version,
                "source": tl.source,
            }
        )

    # No whole-corpus total (paging contract rule 2): has_next already came off the +1 sentinel above.
    pagination = Page[tuple[Tracklist, FileRecord]](page=page, page_size=page_size, has_next=has_next)

    context: dict[str, Any] = {
        "request": request,
        "current_page": "cue",
        "stats": stats,
        "tracklists": tracklists,
        "pagination": pagination,
    }

    # CUT-02 (Phase 62): the non-HX path already 302-redirected above (SHELL-05), so this is
    # reached only for HX rail swaps -- the LIVE shell pagination/filter/sort fragment (D-03b).
    return templates.TemplateResponse(request=request, name="cue/partials/cue_list.html", context=context)


@router.post("/{tracklist_id}/generate", response_class=HTMLResponse)
async def generate_cue(
    request: Request,
    tracklist_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Generate a CUE file for a specific tracklist."""
    tracklist, file_record = await _load_tracklist_with_file(session, tracklist_id)

    if tracklist is None:
        return HTMLResponse(content="Tracklist not found", status_code=404)

    # Validate the file is applied (READ-05/D-01: an executed proposal exists, NOT files.state).
    if file_record is None or not await is_applied(session, file_record.id):
        toast_msg = "File must be executed before generating a CUE sheet. Run the pipeline to move the file to its destination."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    # Validate tracklist is approved
    if tracklist.status != "approved":
        toast_msg = "Tracklist must be approved before generating a CUE sheet."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    # Build CUE tracks
    if not tracklist.latest_version_id:
        toast_msg = "No tracks have timestamps. CUE sheets require track timing data from fingerprinting or 1001Tracklists."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    cue_tracks = await _build_cue_tracks(session, tracklist.latest_version_id)

    # Validate at least one track has timestamps
    if not any(t.timestamp_seconds is not None for t in cue_tracks):
        toast_msg = "No tracks have timestamps. CUE sheets require track timing data from fingerprinting or 1001Tracklists."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    # Generate and write CUE file
    audio_path = Path(file_record.current_path)
    try:
        content = generate_cue_content(audio_path.name, file_record.file_type, cue_tracks)
        written_path = write_cue_file(content, audio_path)
    except Exception as exc:
        toast_msg = f"Failed to write CUE file: {exc}. Check filesystem permissions on the destination directory."
        return await _render_generate_error(request, session, tracklist, file_record, toast_msg)

    # Determine version for toast message
    cue_version = _get_cue_version(file_record.current_path)
    toast_msg = f"CUE file regenerated: {written_path.name} (v{cue_version})" if cue_version > 1 else f"CUE file generated: {written_path.name}"

    # Return updated row + OOB toast
    track_count = await _get_track_count(session, tracklist.latest_version_id)

    # Detect which surface the request came from via HX-Target.
    hx_target = request.headers.get("HX-Target", "")

    # phaze-js16: the v7 cue-workspace card's APPROVE targets #cue-card-{id} -- mirror the
    # cue-card- branch _render_generate_error already has (phaze-2w49) so a SUCCESSFUL approve
    # re-renders the same _cue_preview.html card instead of falling through to the legacy
    # cue/partials/cue_row.html markup. The write just succeeded, so the card stays eligible with
    # a fresh in-memory preview of the CUE we just wrote (no extra query needed -- `content` IS it).
    if hx_target.startswith("cue-card-"):
        card = {
            "tracklist_id": tracklist.id,
            "set_name": audio_path.stem,
            "eligible": True,
            "cue_text": content,
        }
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_cue_preview.html",
            context={"request": request, "card": card, "toast_message": toast_msg},
        )

    if hx_target.startswith("tracklist-"):
        # Request came from tracklist card -- return updated card.
        # phaze-fig9: tracklist_card.html renders its track-count chip from the derived
        # `tracklist._track_count` attribute (Undefined -> 0 on a bare ORM object), so attach the
        # count we already computed above instead of letting the swapped-in card falsely read "0
        # tracks". Also attach `_cue_version` for the same reason `_attach_track_count` exists --
        # consistency with every other tracklist_card.html-rendering route.
        tracklist._track_count = track_count  # type: ignore[attr-defined]
        tracklist._cue_version = cue_version  # type: ignore[attr-defined]
        return templates.TemplateResponse(
            request=request,
            name="tracklists/partials/tracklist_card.html",
            context={
                "request": request,
                "tracklist": tracklist,
                "cue_version": cue_version,
                "toast_message": toast_msg,
            },
        )

    row_data: dict[str, Any] = {
        "id": tracklist.id,
        "artist": tracklist.artist or "Unknown Artist",
        "event": tracklist.event or "",
        "date": tracklist.date,
        "track_count": track_count,
        "cue_version": cue_version,
        "source": tracklist.source,
    }

    return templates.TemplateResponse(
        request=request,
        name="cue/partials/cue_row.html",
        context={
            "request": request,
            "tracklist": row_data,
            "toast_message": toast_msg,
        },
    )


@router.post("/generate-batch", response_class=HTMLResponse)
async def generate_batch(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Batch-generate CUE files for all eligible tracklists.

    phaze-8lpg: restructured from three full-corpus passes (the generation loop, a second full
    re-materialization of the eligible set for the response, and ``_get_cue_stats``' own full pass)
    down to exactly ONE full-corpus materialization -- the generation loop itself, which by
    definition must visit every eligible file to generate its CUE. The response then renders a
    single BOUNDED page through the SAME ``paged_stmt`` contract :func:`list_cue` uses, instead of
    re-querying and rendering the whole corpus a second time behind a fabricated
    ``Pagination(page=1, total=...)``. Each per-file disk write (:func:`write_cue_file`: synchronous
    ``exists()``/``iterdir()`` probes plus ``open()``/``write()`` against the media mount) is
    offloaded via ``asyncio.to_thread`` so the event loop is free between files rather than frozen
    for the write's duration.
    """
    eligible_pairs = await _get_eligible_tracklist_query(session)
    generated_count = 0

    for tl, fr in eligible_pairs:
        if not tl.latest_version_id:
            continue

        cue_tracks = await _build_cue_tracks(session, tl.latest_version_id)
        if not any(t.timestamp_seconds is not None for t in cue_tracks):
            continue

        audio_path = Path(fr.current_path)
        try:
            content = generate_cue_content(audio_path.name, fr.file_type, cue_tracks)
            # phaze-8lpg: write_cue_file's next_cue_path() exists()/iterdir() probes plus the
            # open()/write() itself are synchronous, blocking media-mount I/O -- offload to a
            # worker thread instead of running them inline on the event loop.
            await asyncio.to_thread(write_cue_file, content, audio_path)
            generated_count += 1
        except Exception:
            logger.exception("Failed to generate CUE for tracklist %s", tl.id)
            continue

    toast_msg = f"Generated {generated_count} CUE files"

    # phaze-8lpg: compute stats from the eligible_pairs ALREADY materialized above (the generation
    # loop) instead of calling _get_cue_stats (which would re-run _get_eligible_tracklist_query),
    # then render exactly ONE bounded page -- the SAME paged_stmt path list_cue uses -- instead of
    # re-materializing and rendering the whole corpus a second time.
    stats = await _cue_stats_from_eligible_pairs(session, eligible_pairs)
    page_size = DEFAULT_PAGE_SIZE
    try:
        async with session.begin_nested():
            stmt = paged_stmt(
                _eligible_tracklist_stmt(),
                page=1,
                page_size=page_size,
                order_by=_ELIGIBLE_DISPLAY_ORDER,
                tiebreaker=(Tracklist.id,),
            )
            raw = (await session.execute(stmt)).tuples().all()
    except Exception:
        logger.warning("cue_generate_batch_render_degraded", exc_info=True)
        raw = []
    page_pairs, has_next = split_sentinel(list(raw), page_size)

    tracklists: list[dict[str, Any]] = []
    for tl, fr in page_pairs:
        cue_version = _get_cue_version(fr.current_path)
        track_count = await _get_track_count(session, tl.latest_version_id)

        tracklists.append(
            {
                "id": tl.id,
                "artist": tl.artist or "Unknown Artist",
                "event": tl.event or "",
                "date": tl.date,
                "track_count": track_count,
                "cue_version": cue_version,
                "source": tl.source,
            }
        )

    # No whole-corpus total (mirrors list_cue -- paging contract rule 2): has_next comes off the
    # page_size + 1 sentinel above, not a COUNT over the full eligible set.
    pagination = Page[tuple[Tracklist, FileRecord]](page=1, page_size=page_size, has_next=has_next)

    return templates.TemplateResponse(
        request=request,
        name="cue/partials/cue_list.html",
        context={
            "request": request,
            "tracklists": tracklists,
            "stats": stats,
            "pagination": pagination,
            "toast_message": toast_msg,
        },
    )


async def _get_track_count(session: AsyncSession, version_id: uuid.UUID | None) -> int:
    """Count tracks for a tracklist version (0 if there is none)."""
    if not version_id:
        return 0
    count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == version_id))
    return count_result.scalar() or 0


async def _build_generate_error_card(
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord | None,
) -> dict[str, Any]:
    """Rebuild the pipeline preview card's context after a failed generate (phaze-2w49).

    Mirrors the eligibility rule in ``services.review.get_cue_review_cards`` for a single
    tracklist so the re-rendered card reflects the tracklist's REAL current state: the write-
    failure branch stays eligible (a genuine retry, matching the error message's own promise),
    while a data-gap branch (not applied/approved/timestamped) renders the honest gated state
    instead of a stale APPROVE button.
    """
    eligible = False
    cue_text: str | None = None
    if file_record is not None and tracklist.status == "approved" and tracklist.latest_version_id and await is_applied(session, file_record.id):
        cue_tracks = await _build_cue_tracks(session, tracklist.latest_version_id)
        if any(t.timestamp_seconds is not None for t in cue_tracks):
            eligible = True
            cue_text = generate_cue_content(Path(file_record.current_path).name, file_record.file_type, cue_tracks)

    return {
        "tracklist_id": tracklist.id,
        "set_name": Path(file_record.current_path).stem if file_record is not None else str(tracklist.id),
        "eligible": eligible,
        "cue_text": cue_text,
    }


async def _render_generate_error(
    request: Request,
    session: AsyncSession,
    tracklist: Tracklist,
    file_record: FileRecord | None,
    message: str,
) -> HTMLResponse:
    """Re-render the surface a failed ``/generate`` targeted, with the error as an OOB toast.

    phaze-2w49: htmx's oobSwap strips the OOB toast element from the response fragment
    unconditionally, then runs the PRIMARY ``outerHTML`` swap against the now-empty remainder --
    with no empty-guard, ``swapOuterHTML`` inserts nothing and calls ``target.remove()``. A
    toast-only 200 therefore deletes the very card/row the toast is complaining about on all three
    generate surfaces (the pipeline preview card, the tracklist card, and the cue row). Every error
    branch must re-render its own primary content alongside the toast instead.
    """
    hx_target = request.headers.get("HX-Target", "")
    cue_version = _get_cue_version(file_record.current_path) if file_record is not None else 0

    if hx_target.startswith("cue-card-"):
        card = await _build_generate_error_card(session, tracklist, file_record)
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/_cue_preview.html",
            context={"request": request, "card": card, "toast_message": message},
        )

    if hx_target.startswith("tracklist-"):
        return templates.TemplateResponse(
            request=request,
            name="tracklists/partials/tracklist_card.html",
            context={"request": request, "tracklist": tracklist, "cue_version": cue_version, "toast_message": message},
        )

    row_data: dict[str, Any] = {
        "id": tracklist.id,
        "artist": tracklist.artist or "Unknown Artist",
        "event": tracklist.event or "",
        "date": tracklist.date,
        "track_count": await _get_track_count(session, tracklist.latest_version_id),
        "cue_version": cue_version,
        "source": tracklist.source,
    }
    return templates.TemplateResponse(
        request=request,
        name="cue/partials/cue_row.html",
        context={"request": request, "tracklist": row_data, "toast_message": message},
    )
