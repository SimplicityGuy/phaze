"""Tag review UI router -- side-by-side comparison, inline editing, and tag writing."""

from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.services.proposal_queries import Pagination
from phaze.services.tag_proposal import CORE_FIELDS, compute_proposed_tags
from phaze.services.tag_writer import execute_tag_write


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/tags", tags=["tags"])

FIELD_LABELS: dict[str, str] = {
    "artist": "Artist",
    "title": "Title",
    "album": "Album",
    "year": "Year",
    "genre": "Genre",
    "track_number": "Track #",
}

VALID_FIELDS = set(CORE_FIELDS)


async def _get_tag_stats(session: AsyncSession) -> dict[str, int]:
    """Count pending, completed, and discrepancy files for tag writing."""
    # Count EXECUTED files (potential tag write targets)
    executed_stmt = select(func.count(FileRecord.id)).where(FileRecord.state == FileState.EXECUTED)
    executed_result = await session.execute(executed_stmt)
    total_executed = executed_result.scalar() or 0

    # Count completed writes
    completed_stmt = select(func.count(func.distinct(TagWriteLog.file_id))).where(TagWriteLog.status == TagWriteStatus.COMPLETED)
    completed_result = await session.execute(completed_stmt)
    completed = completed_result.scalar() or 0

    # Count discrepancy writes
    discrepancy_stmt = select(func.count(func.distinct(TagWriteLog.file_id))).where(TagWriteLog.status == TagWriteStatus.DISCREPANCY)
    discrepancy_result = await session.execute(discrepancy_stmt)
    discrepancies = discrepancy_result.scalar() or 0

    pending = total_executed - completed - discrepancies

    return {"pending": max(pending, 0), "completed": completed, "discrepancies": discrepancies}


async def _get_file_with_metadata(session: AsyncSession, file_id: uuid.UUID) -> FileRecord | None:
    """Load a FileRecord with its metadata eagerly loaded."""
    stmt = select(FileRecord).options(selectinload(FileRecord.file_metadata)).where(FileRecord.id == file_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_tracklist_for_file(session: AsyncSession, file_id: uuid.UUID) -> Tracklist | None:
    """Find the tracklist associated with a file."""
    stmt = select(Tracklist).where(Tracklist.file_id == file_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_accepted_discogs_link(session: AsyncSession, file_id: uuid.UUID) -> DiscogsLink | None:
    """Find the accepted DiscogsLink for the file's tracklist, if any."""
    tl_stmt = select(Tracklist.latest_version_id).where(Tracklist.file_id == file_id)
    tl_result = await session.execute(tl_stmt)
    version_id = tl_result.scalar_one_or_none()
    if version_id is None:
        return None
    track_ids = select(TracklistTrack.id).where(TracklistTrack.version_id == version_id)
    link_stmt = (
        select(DiscogsLink)
        .where(DiscogsLink.track_id.in_(track_ids), DiscogsLink.status == "accepted")
        .order_by(DiscogsLink.confidence.desc())
        .limit(1)
    )
    link_result = await session.execute(link_stmt)
    return link_result.scalar_one_or_none()


async def _get_latest_write_log(session: AsyncSession, file_id: uuid.UUID) -> TagWriteLog | None:
    """Get the most recent TagWriteLog for a file."""
    stmt = select(TagWriteLog).where(TagWriteLog.file_id == file_id).order_by(TagWriteLog.written_at.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _build_comparison(
    file_metadata: FileMetadata | None,
    proposed_tags: dict[str, str | int | None],
) -> list[dict[str, Any]]:
    """Build comparison list for all CORE_FIELDS."""
    comparison = []
    for field in CORE_FIELDS:
        current_val = getattr(file_metadata, field, None) if file_metadata else None
        proposed_val = proposed_tags.get(field)
        changed = (
            str(current_val) != str(proposed_val)
            if current_val is not None and proposed_val is not None
            else (current_val is not None) != (proposed_val is not None)
        )
        comparison.append(
            {
                "field": field,
                "label": FIELD_LABELS.get(field, field),
                "current": current_val,
                "proposed": proposed_val,
                "changed": changed,
            }
        )
    return comparison


def _count_changes(comparison: list[dict[str, Any]]) -> int:
    """Count number of changed fields in a comparison."""
    return sum(1 for c in comparison if c["changed"])


def _qualifies_for_bulk_write(comparison: list[dict[str, Any]]) -> bool:
    """LOCKED D-03 / OQ-1 predicate for the no-discrepancies bulk tag write.

    A file qualifies iff its server-computed comparison has ``>= 1`` changed field (there IS
    something to write) AND no field would blank an existing tag (``current is not None and
    proposed is None``) -- a bulk write NEVER erases an existing tag. Files failing either clause
    stay per-file Approve/Edit/Skip.

    The blank clause is defensive: ``compute_proposed_tags`` copies every non-None metadata field
    into the proposal, so a server-computed comparison never blanks a tag. The guard makes that
    invariant explicit + future-proof, and is asserted directly at the unit level.
    """
    if _count_changes(comparison) < 1:
        return False
    return not any(c["current"] is not None and c["proposed"] is None for c in comparison)


def _determine_file_status(write_log: TagWriteLog | None) -> str:
    """Determine the tag write status for a file."""
    if write_log is None:
        return "pending"
    return write_log.status


@router.get("/", response_class=HTMLResponse)
async def list_tags(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=10, le=100),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the tag review list page or HTMX partial."""
    # SHELL-05 (D-03): a plain (non-HX) GET / bookmark resolves into the v7.0 shell.
    # The in-page HX filter branch below is left intact so the app stays usable (D-01).
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/s/tagwrite", status_code=302)

    # Query EXECUTED files with metadata
    stmt = (
        select(FileRecord)
        .options(selectinload(FileRecord.file_metadata))
        .where(FileRecord.state == FileState.EXECUTED)
        .order_by(FileRecord.original_filename)
    )

    # Count total
    count_stmt = select(func.count(FileRecord.id)).where(FileRecord.state == FileState.EXECUTED)
    count_result = await session.execute(count_stmt)
    total = count_result.scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)

    result = await session.execute(stmt)
    file_records = list(result.scalars().all())

    # Build file data with proposed tags and status
    files: list[dict[str, Any]] = []
    for fr in file_records:
        tracklist = await _get_tracklist_for_file(session, fr.id)
        discogs_link = await _get_accepted_discogs_link(session, fr.id)
        proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename, discogs_link=discogs_link)
        write_log = await _get_latest_write_log(session, fr.id)
        comparison = _build_comparison(fr.file_metadata, proposed)
        changes = _count_changes(comparison)
        status = _determine_file_status(write_log)

        files.append(
            {
                "id": fr.id,
                "filename": fr.original_filename,
                "file_type": fr.file_type,
                "changes": changes,
                "status": status,
                "comparison": comparison,
            }
        )

    stats = await _get_tag_stats(session)
    pagination = Pagination(page=page, page_size=page_size, total=total)

    context: dict[str, Any] = {
        "request": request,
        "current_page": "tags",
        "files": files,
        "stats": stats,
        "pagination": pagination,
    }

    # CUT-02 (Phase 62): the non-HX path already 302-redirected above (SHELL-05), so this is
    # reached only for HX rail swaps -- the LIVE shell pagination/filter/sort fragment (D-03b).
    return templates.TemplateResponse(request=request, name="tags/partials/tag_list.html", context=context)


@router.get("/{file_id}/compare", response_class=HTMLResponse)
async def compare_tags(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the tag comparison panel for a file."""
    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        return HTMLResponse(content="File not found", status_code=404)

    tracklist = await _get_tracklist_for_file(session, file_id)
    discogs_link = await _get_accepted_discogs_link(session, file_id)
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=discogs_link)
    comparison = _build_comparison(file_record.file_metadata, proposed)

    return templates.TemplateResponse(
        request=request,
        name="tags/partials/tag_comparison.html",
        context={
            "request": request,
            "file": file_record,
            "comparison": comparison,
            "proposed_tags": proposed,
            "field_labels": FIELD_LABELS,
        },
    )


@router.get("/{file_id}/edit/{field}", response_class=HTMLResponse)
async def edit_tag_field(
    request: Request,
    file_id: uuid.UUID,
    field: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return inline edit input for a proposed tag field."""
    if field not in VALID_FIELDS:
        return HTMLResponse(content="Invalid field", status_code=400)

    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        return HTMLResponse(content="File not found", status_code=404)

    tracklist = await _get_tracklist_for_file(session, file_id)
    discogs_link = await _get_accepted_discogs_link(session, file_id)
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=discogs_link)
    value = proposed.get(field, "")

    return templates.TemplateResponse(
        request=request,
        name="tags/partials/inline_edit.html",
        context={
            "request": request,
            "file_id": file_id,
            "field": field,
            "value": value,
            "label": FIELD_LABELS.get(field, field),
        },
    )


@router.put("/{file_id}/edit/{field}", response_class=HTMLResponse)
async def save_tag_field(
    request: Request,
    file_id: uuid.UUID,
    field: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save an inline edit for a proposed tag field and return display span."""
    if field not in VALID_FIELDS:
        return HTMLResponse(content="Invalid field", status_code=400)

    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        return HTMLResponse(content="File not found", status_code=404)

    form_data = await request.form()
    new_value = str(form_data.get(field, ""))

    # Compute original proposed to determine if changed
    current_val = getattr(file_record.file_metadata, field, None) if file_record.file_metadata else None
    changed = str(new_value) != str(current_val) if current_val is not None and new_value else (current_val is not None) != bool(new_value)

    return templates.TemplateResponse(  # nosemgrep: python.fastapi.web.tainted-direct-response-fastapi.tainted-direct-response-fastapi -- Jinja2 TemplateResponse is autoescaped; no raw/`| safe` interpolation of the form value, so this is not a direct tainted response.
        request=request,
        name="tags/partials/inline_display.html",
        context={
            "request": request,
            "file_id": file_id,
            "field": field,
            "value": new_value,
            "changed": changed,
        },
    )


@router.post("/{file_id}/write", response_class=HTMLResponse)
async def write_file_tags(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Execute tag write for a file using form-submitted proposed values."""
    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        return HTMLResponse(content="File not found", status_code=404)

    if file_record.state != FileState.EXECUTED:
        return HTMLResponse(content="Only executed files can have tags written", status_code=400)

    form_data = await request.form()

    # Build tags dict from form data
    tags: dict[str, str | int | None] = {}
    for field in CORE_FIELDS:
        val = form_data.get(field)
        if val is not None and str(val).strip():
            if field in ("year", "track_number"):
                try:
                    tags[field] = int(str(val))
                except (ValueError, TypeError):
                    tags[field] = str(val)
            else:
                tags[field] = str(val)

    # Fallback: if no tag values submitted (e.g., collapsed row button without comparison panel),
    # use server-computed proposed tags
    tracklist = await _get_tracklist_for_file(session, file_id)
    discogs_link = await _get_accepted_discogs_link(session, file_id)
    if not tags:
        computed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=discogs_link)
        tags = {k: v for k, v in computed.items() if v is not None}
        source = "proposal"
    else:
        computed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=discogs_link)
        has_edits = any(str(tags.get(f, "")) != str(computed.get(f, "")) for f in CORE_FIELDS if f in tags or f in computed)
        source = "manual_edit" if has_edits else "proposal"

    try:
        log_entry = await execute_tag_write(session, file_record, tags, source)
        await session.commit()

        status = log_entry.status
        if status == TagWriteStatus.COMPLETED:
            toast_message = f"Tags written to {file_record.original_filename}"
        elif status == TagWriteStatus.DISCREPANCY:
            disc_count = len(log_entry.discrepancies) if log_entry.discrepancies else 0
            toast_message = f"Tags written with {disc_count} discrepancy. Re-read values differ from what was sent -- usually encoding normalization. Review the audit log for details."
        else:
            toast_message = f"Tag write failed: {log_entry.error_message or 'Unknown error'}. The file may be read-only or corrupted. Check file permissions and try again."
    except ValueError as exc:
        status = "failed"
        toast_message = f"Tag write failed: {exc}"

    # Rebuild file data for the updated row
    comparison = _build_comparison(file_record.file_metadata, tags)
    changes = _count_changes(comparison)

    return templates.TemplateResponse(  # nosemgrep: python.fastapi.web.tainted-direct-response-fastapi.tainted-direct-response-fastapi -- Jinja2 TemplateResponse is autoescaped; the toast/comparison values are escaped on render (no raw/`| safe`), so this is not a direct tainted response.
        request=request,
        name="tags/partials/tag_row.html",
        context={
            "request": request,
            "file": {
                "id": file_record.id,
                "filename": file_record.original_filename,
                "file_type": file_record.file_type,
                "changes": changes,
                "status": status,
            },
            "toast_message": toast_message,
        },
    )


@router.post("/bulk-write-no-discrepancies", response_class=HTMLResponse)
async def bulk_write_no_discrepancies(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-02 (D-03 / OQ-1): write tags for every qualifying EXECUTED file, server-re-queried.

    Mirrors ``tracklists.reject_low_confidence`` discipline -- the server re-queries the candidate
    set at submit (EXECUTED files with metadata that have NO COMPLETED ``TagWriteLog``) and applies
    the LOCKED D-03 / OQ-1 predicate (:func:`_qualifies_for_bulk_write`): ``>= 1`` changed field AND
    no field that would blank an existing tag. It reads NO client-supplied id-list, so a stale or
    forged selection can never mass-apply. Non-qualifying files stay per-file Approve/Edit/Skip.
    Each qualifying file is written via the EXISTING :func:`execute_tag_write` (no new apply logic).
    """
    completed_subq = select(TagWriteLog.file_id).where(TagWriteLog.status == TagWriteStatus.COMPLETED)
    stmt = (
        select(FileRecord)
        .options(selectinload(FileRecord.file_metadata))
        .where(FileRecord.state == FileState.EXECUTED, FileRecord.id.not_in(completed_subq))
        .order_by(FileRecord.original_filename)
    )
    file_records = list((await session.execute(stmt)).scalars().all())

    written = 0
    for fr in file_records:
        tracklist = await _get_tracklist_for_file(session, fr.id)
        discogs_link = await _get_accepted_discogs_link(session, fr.id)
        proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename, discogs_link=discogs_link)
        comparison = _build_comparison(fr.file_metadata, proposed)
        if not _qualifies_for_bulk_write(comparison):
            continue
        tags: dict[str, str | int | None] = {k: v for k, v in proposed.items() if v is not None}
        await execute_tag_write(session, fr, tags, source="proposal")
        written += 1
    await session.commit()

    stats = await _get_tag_stats(session)
    toast_message = (
        f"{written} files tagged (no discrepancies)."
        if written
        else "Nothing matched -- no executed files qualify for a no-discrepancy bulk write right now."
    )
    return templates.TemplateResponse(
        request=request,
        name="tags/partials/bulk_write_response.html",
        context={"request": request, "stats": stats, "written": written, "toast_message": toast_message},
    )


@router.post("/{file_id}/undo", response_class=HTMLResponse)
async def undo_tag_write(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-05 (D-04): revert a tag write by re-applying ``TagWriteLog.before_tags``.

    Reuses the EXISTING :func:`execute_tag_write` mutagen path (``source="undo"``) to restore the
    snapshot captured before the latest write -- NO new apply/undo logic. Returns 404 when the file
    has no prior write log. Appends one further ``TagWriteLog`` so the append-only audit trail stays
    coherent (REVIEW-05: every apply, including a reversal, is one audit row).
    """
    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        return HTMLResponse(content="File not found", status_code=404)

    latest = await _get_latest_write_log(session, file_id)
    if latest is None:
        return HTMLResponse(content="No prior tag write to undo", status_code=404)

    log_entry = await execute_tag_write(session, file_record, latest.before_tags, source="undo")
    await session.commit()

    # Rebuild the row for the outerHTML swap.
    tracklist = await _get_tracklist_for_file(session, file_id)
    discogs_link = await _get_accepted_discogs_link(session, file_id)
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=discogs_link)
    comparison = _build_comparison(file_record.file_metadata, proposed)
    changes = _count_changes(comparison)

    return templates.TemplateResponse(
        request=request,
        name="tags/partials/tag_row.html",
        context={
            "request": request,
            "file": {
                "id": file_record.id,
                "filename": file_record.original_filename,
                "file_type": file_record.file_type,
                "changes": changes,
                "status": log_entry.status,
            },
            "toast_message": f"Reverted tags for {file_record.original_filename}.",
        },
    )
