"""CUE sheet management UI router -- generation, batch generation, and CUE management page."""

import logging
from pathlib import Path
import re
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord, FileState
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.cue_generator import CueTrackData, generate_cue_content, parse_timestamp_string, write_cue_file
from phaze.services.proposal_queries import Pagination


logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/cue", tags=["cue"])


async def _get_eligible_tracklist_query(session: AsyncSession) -> list[tuple[Tracklist, FileRecord]]:
    """Query approved tracklists with EXECUTED files that have at least one timestamped track."""
    # Subquery: tracklist IDs that have at least one track with a timestamp
    has_timestamp_subq = (
        select(TracklistVersion.tracklist_id)
        .join(TracklistTrack, TracklistTrack.version_id == TracklistVersion.id)
        .where(TracklistTrack.timestamp.is_not(None))
        .distinct()
        .correlate(Tracklist)
    )

    stmt = (
        select(Tracklist, FileRecord)
        .join(FileRecord, Tracklist.file_id == FileRecord.id)
        .where(
            Tracklist.status == "approved",
            Tracklist.file_id.is_not(None),
            FileRecord.state == FileState.EXECUTED,
            Tracklist.id.in_(has_timestamp_subq),
        )
        .order_by(
            # Fingerprint first per D-02 (CUE-01 preference)
            (Tracklist.source == "fingerprint").desc(),
            Tracklist.artist,
            Tracklist.event,
        )
    )

    result = await session.execute(stmt)
    return list(result.tuples().all())


async def _get_cue_stats(session: AsyncSession) -> dict[str, int]:
    """Compute CUE generation statistics."""
    # Eligible: approved + EXECUTED file + has timestamps
    eligible_pairs = await _get_eligible_tracklist_query(session)
    eligible = len(eligible_pairs)

    # Generated: count of eligible whose file has a .cue on disk
    generated = 0
    for _tl, fr in eligible_pairs:
        if _get_cue_version(fr.current_path) > 0:
            generated += 1

    # Missing timestamps: approved + EXECUTED file but NO tracks with timestamps
    has_timestamp_subq = (
        select(TracklistVersion.tracklist_id)
        .join(TracklistTrack, TracklistTrack.version_id == TracklistVersion.id)
        .where(TracklistTrack.timestamp.is_not(None))
        .distinct()
    )

    missing_stmt = (
        select(func.count(Tracklist.id))
        .join(FileRecord, Tracklist.file_id == FileRecord.id)
        .where(
            Tracklist.status == "approved",
            Tracklist.file_id.is_not(None),
            FileRecord.state == FileState.EXECUTED,
            Tracklist.id.not_in(has_timestamp_subq),
        )
    )
    missing_result = await session.execute(missing_stmt)
    missing_timestamps = missing_result.scalar() or 0

    return {"eligible": eligible, "generated": generated, "missing_timestamps": missing_timestamps}


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
    page_size: int = Query(20, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the CUE management page or HTMX partial."""
    stats = await _get_cue_stats(session)

    # Query eligible tracklists for the list
    eligible_pairs = await _get_eligible_tracklist_query(session)

    # Paginate
    total = len(eligible_pairs)
    offset = (page - 1) * page_size
    page_pairs = eligible_pairs[offset : offset + page_size]

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

    pagination = Pagination(page=page, page_size=page_size, total=total)

    context: dict[str, Any] = {
        "request": request,
        "current_page": "cue",
        "stats": stats,
        "tracklists": tracklists,
        "pagination": pagination,
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request=request, name="cue/partials/cue_list.html", context=context)

    return templates.TemplateResponse(request=request, name="cue/list.html", context=context)


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

    # Validate file state
    if file_record is None or file_record.state != FileState.EXECUTED:
        toast_msg = "File must be executed before generating a CUE sheet. Run the pipeline to move the file to its destination."
        return _render_error_toast(request, toast_msg)

    # Validate tracklist is approved
    if tracklist.status != "approved":
        toast_msg = "Tracklist must be approved before generating a CUE sheet."
        return _render_error_toast(request, toast_msg)

    # Build CUE tracks
    if not tracklist.latest_version_id:
        toast_msg = "No tracks have timestamps. CUE sheets require track timing data from fingerprinting or 1001Tracklists."
        return _render_error_toast(request, toast_msg)

    cue_tracks = await _build_cue_tracks(session, tracklist.latest_version_id)

    # Validate at least one track has timestamps
    if not any(t.timestamp_seconds is not None for t in cue_tracks):
        toast_msg = "No tracks have timestamps. CUE sheets require track timing data from fingerprinting or 1001Tracklists."
        return _render_error_toast(request, toast_msg)

    # Generate and write CUE file
    audio_path = Path(file_record.current_path)
    try:
        content = generate_cue_content(audio_path.name, file_record.file_type, cue_tracks)
        written_path = write_cue_file(content, audio_path)
    except Exception as exc:
        toast_msg = f"Failed to write CUE file: {exc}. Check filesystem permissions on the destination directory."
        return _render_error_toast(request, toast_msg)

    # Determine version for toast message
    cue_version = _get_cue_version(file_record.current_path)
    toast_msg = f"CUE file regenerated: {written_path.name} (v{cue_version})" if cue_version > 1 else f"CUE file generated: {written_path.name}"

    # Return updated row + OOB toast
    track_count = 0
    if tracklist.latest_version_id:
        count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tracklist.latest_version_id))
        track_count = count_result.scalar() or 0

    # Detect if request came from tracklist card (HX-Target starts with "tracklist-")
    hx_target = request.headers.get("HX-Target", "")
    if hx_target.startswith("tracklist-"):
        # Request came from tracklist card -- return updated card
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
    """Batch-generate CUE files for all eligible tracklists."""
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
            write_cue_file(content, audio_path)
            generated_count += 1
        except Exception:
            logger.exception("Failed to generate CUE for tracklist %s", tl.id)
            continue

    toast_msg = f"Generated {generated_count} CUE files"

    # Re-query for updated list
    stats = await _get_cue_stats(session)
    eligible_pairs = await _get_eligible_tracklist_query(session)

    tracklists: list[dict[str, Any]] = []
    for tl, fr in eligible_pairs:
        cue_version = _get_cue_version(fr.current_path)
        track_count = 0
        if tl.latest_version_id:
            count_result = await session.execute(select(func.count(TracklistTrack.id)).where(TracklistTrack.version_id == tl.latest_version_id))
            track_count = count_result.scalar() or 0

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

    pagination = Pagination(page=1, page_size=20, total=len(eligible_pairs))

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


def _render_error_toast(request: Request, message: str) -> HTMLResponse:
    """Return an error toast via OOB swap."""
    return templates.TemplateResponse(
        request=request,
        name="cue/partials/toast.html",
        context={
            "request": request,
            "message": message,
        },
    )
