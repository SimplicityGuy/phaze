"""Tag review UI router -- side-by-side comparison, inline editing, and tag writing."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.file import FileRecord, FileState
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist
from phaze.services.proposal_queries import Pagination
from phaze.services.tag_proposal import CORE_FIELDS, compute_proposed_tags
from phaze.services.tag_writer import execute_tag_write


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.metadata import FileMetadata


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
) -> HTMLResponse:
    """Render the tag review list page or HTMX partial."""
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
        proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename)
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

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request=request, name="tags/partials/tag_list.html", context=context)

    return templates.TemplateResponse(request=request, name="tags/list.html", context=context)


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
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename)
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
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename)
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

    return templates.TemplateResponse(
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
    if not tags:
        tracklist = await _get_tracklist_for_file(session, file_id)
        computed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename)
        tags = {k: v for k, v in computed.items() if v is not None}
        source = "proposal"
    else:
        tracklist = await _get_tracklist_for_file(session, file_id)
        computed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename)
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
                "status": status,
            },
            "toast_message": toast_message,
        },
    )
