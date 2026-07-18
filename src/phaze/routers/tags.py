"""Tag review UI router -- side-by-side comparison, inline editing, and tag writing."""

from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.services.proposal_queries import Pagination
from phaze.services.stage_status import applied_clause, is_applied
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

# D-03: bound the operator-triggered no-discrepancy bulk loop. Reviving the applied() gate can make a
# large first-time-visible applied backlog suddenly enumerable; cap one submit at a batch of this size
# (low-thousands, consistent with in-tree page bounds) so the loop cannot blow up at 200K scale.
_MAX_BULK_TAG_WRITE = 2000

# WR-01: statuses that make an applied file TERMINAL for the tag-write queue -- it has either been
# written (COMPLETED) or determined to need no write (NO_OP). Both are excluded from the candidate
# window so neither can re-occupy the alphabetically-first ``.limit()`` slots and starve qualifying
# files. DISCREPANCY is intentionally NOT terminal (the file re-appears so the operator can retry).
_TERMINAL_TAGWRITE_STATUSES = (TagWriteStatus.COMPLETED, TagWriteStatus.NO_OP)


def _terminal_tagwrite_subq() -> Select[tuple[uuid.UUID]]:
    """Subquery of ``file_id``\\ s with a TERMINAL ``TagWriteLog`` (COMPLETED or NO_OP).

    The single source of the tag-write idempotency anti-join, shared by both operator builders
    (``bulk_write_no_discrepancies`` here and ``services.review.get_tagwrite_review_rows``): a file
    listed here is done (written) or needs no write (zero-change NO_OP) and is dropped from the
    candidate window (WR-01).
    """
    return select(TagWriteLog.file_id).where(TagWriteLog.status.in_(_TERMINAL_TAGWRITE_STATUSES))


async def _get_tag_stats(session: AsyncSession) -> dict[str, int]:
    """Count pending, completed, and discrepancy files for tag writing."""
    # Count applied files (potential tag write targets -- an executed proposal exists, READ-05/D-01)
    executed_stmt = select(func.count(FileRecord.id)).where(applied_clause())
    executed_result = await session.execute(executed_stmt)
    total_executed = executed_result.scalar() or 0

    # Count completed writes (distinct files -- display cell)
    completed_stmt = select(func.count(func.distinct(TagWriteLog.file_id))).where(TagWriteLog.status == TagWriteStatus.COMPLETED)
    completed_result = await session.execute(completed_stmt)
    completed = completed_result.scalar() or 0

    # Count discrepancy writes (distinct files -- display cell)
    discrepancy_stmt = select(func.count(func.distinct(TagWriteLog.file_id))).where(TagWriteLog.status == TagWriteStatus.DISCREPANCY)
    discrepancy_result = await session.execute(discrepancy_stmt)
    discrepancies = discrepancy_result.scalar() or 0

    # WR-02: count each already-handled file ONCE. A single file can carry BOTH a COMPLETED and a
    # DISCREPANCY log (a normal re-write sequence), so subtracting the two independent DISTINCT tallies
    # (``completed`` + ``discrepancies``) double-counts it and under-reports ``pending``. Tally the
    # union of handled statuses over DISTINCT file_id instead, so ``pending`` is exact. WR-01: a
    # NO_OP file is terminally resolved (zero changes -- nothing to write), so it is handled too.
    handled_stmt = select(func.count(func.distinct(TagWriteLog.file_id))).where(
        TagWriteLog.status.in_((TagWriteStatus.COMPLETED, TagWriteStatus.DISCREPANCY, TagWriteStatus.NO_OP))
    )
    handled_result = await session.execute(handled_stmt)
    handled = handled_result.scalar() or 0

    pending = total_executed - handled

    return {"pending": max(pending, 0), "completed": completed, "discrepancies": discrepancies}


async def _get_file_with_metadata(session: AsyncSession, file_id: uuid.UUID) -> FileRecord | None:
    """Load a FileRecord with its metadata eagerly loaded."""
    stmt = select(FileRecord).options(selectinload(FileRecord.file_metadata)).where(FileRecord.id == file_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_tracklist_for_file(session: AsyncSession, file_id: uuid.UUID) -> Tracklist | None:
    """Find the best tracklist associated with a file.

    ``tracklists.file_id`` has only a NON-unique index, and mainline paths (>=90 auto-link,
    fingerprint re-scan) can legitimately create multiple tracklists per file. A ``scalar_one_or_none``
    here would raise ``MultipleResultsFound`` -> 500 the tags page and silently empty the tagwrite queue
    (services/review.py swallows it). Pick the highest-confidence link deterministically instead, mirroring
    services/pipeline.py's ``max(match_confidence)`` per-file model.
    """
    stmt = select(Tracklist).where(Tracklist.file_id == file_id).order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.id).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def _get_accepted_discogs_link(session: AsyncSession, file_id: uuid.UUID) -> DiscogsLink | None:
    """Find the accepted DiscogsLink for the file's tracklist, if any."""
    # Multiplicity-tolerant (see _get_tracklist_for_file): a file may have >1 tracklist; pick the
    # highest-confidence one's latest version rather than raising MultipleResultsFound.
    tl_stmt = (
        select(Tracklist.latest_version_id)
        .where(Tracklist.file_id == file_id)
        .order_by(Tracklist.match_confidence.desc().nulls_last(), Tracklist.id)
        .limit(1)
    )
    tl_result = await session.execute(tl_stmt)
    version_id = tl_result.scalars().first()
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


def _summarize_tags(comparison: list[dict[str, Any]], side: str) -> str:
    """Join a comparison's ``current`` (before) or ``proposed`` (after) side into a display string.

    Renders ``"label: value · label: value · …"`` across every CORE field, with an em dash for a
    ``None`` value (an absent tag). ``side`` is ``"current"`` or ``"proposed"``. All values are plain
    Python data -- the caller's template autoescapes them on render (T-60-XSS). Shared with
    ``services.review.get_tagwrite_review_rows`` (the tagwrite queue's ``before_summary`` /
    ``after_summary``) so a row's diff text never drifts between the queue and the mutation routes.
    """
    parts = [f"{c['label']}: {c[side] if c[side] is not None else '—'}" for c in comparison]
    return " · ".join(parts)


# phaze-nvll: the v7 tagwrite workspace (tagwrite_workspace.html) renders rows from the shared
# pipeline/partials/_diff_row.html partial and hx-targets each row's own div. write_file_tags and
# undo_tag_write historically always returned the legacy <tr>-based tag_row.html -- which carries
# ZERO undo controls, so the outerHTML swap after APPROVE destroyed the row (and the UNDO button
# that would have reversed it) in the same stroke, and bare 400/404 strings on a stale row (file
# gone / no longer executed / no prior write) were silently dropped by htmx (it does not swap
# non-2xx bodies by default; shell.html only special-cases #record-body). The legacy tag list/
# comparison pages (tag_list.html, tag_comparison.html) target `#row-{file_id}` and must keep
# getting tag_row.html back, so the v7 response is opt-in via the same HX-Target negotiation
# proposals.py uses (phaze-3a2j).
_V7_TAGWRITE_ROW_PREFIX = "tagwrite-row"


def _is_v7_tagwrite_target(request: Request, file_id: uuid.UUID) -> bool:
    """True when the request came from the v7 tagwrite diff-row workspace."""
    return request.headers.get("HX-Target", "") == f"{_V7_TAGWRITE_ROW_PREFIX}-{file_id}"


async def _tagwrite_row_context(session: AsyncSession, file_record: FileRecord, *, row_state: str) -> dict[str, Any]:
    """Build the shared _diff_row.html context for one tagwrite row, at the given lifecycle state."""
    tracklist = await _get_tracklist_for_file(session, file_record.id)
    discogs_link = await _get_accepted_discogs_link(session, file_record.id)
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=discogs_link)
    comparison = _build_comparison(file_record.file_metadata, proposed)
    return {
        "row_id_prefix": _V7_TAGWRITE_ROW_PREFIX,
        "pid": file_record.id,
        "file": file_record.original_filename,
        "original_path": file_record.original_filename,
        "before": _summarize_tags(comparison, "current"),
        "after": _summarize_tags(comparison, "proposed"),
        "approve_url": f"/tags/{file_record.id}/write",
        "approve_method": "post",
        "undo_url": f"/tags/{file_record.id}/undo",
        "undo_method": "post",
        "show_edit": False,
        "show_skip": False,
        "show_undo": True,
        "row_state": row_state,
    }


def _tagwrite_diff_row_response(request: Request, row_context: dict[str, Any], toast_message: str | None) -> HTMLResponse:
    """Render the shared _diff_row.html (tag facet) plus its OOB toast for a v7 row swap."""
    return templates.TemplateResponse(
        request=request,
        name="tags/partials/tagwrite_diff_row.html",
        context={"request": request, "toast_message": toast_message, **row_context},
    )


def _tagwrite_stale_toast_response(request: Request, toast_message: str) -> HTMLResponse:
    """A v7 row whose file has vanished entirely: OOB toast only, status 200 (phaze-nvll defect 3).

    There is no file left to rebuild a row from, so the response's main (non-OOB) body is empty --
    htmx's outerHTML swap then removes the stale row from the DOM -- while the toast still surfaces
    the failure instead of a bare 400/404 string htmx silently drops.
    """
    return templates.TemplateResponse(
        request=request,
        name="tags/partials/toast.html",
        context={"request": request, "toast_message": toast_message},
    )


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

    # Query applied files with metadata (an executed proposal exists, READ-05/D-01)
    stmt = select(FileRecord).options(selectinload(FileRecord.file_metadata)).where(applied_clause()).order_by(FileRecord.original_filename)

    # Count total
    count_stmt = select(func.count(FileRecord.id)).where(applied_clause())
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
    is_v7 = _is_v7_tagwrite_target(request, file_id)

    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        # phaze-nvll defect 3: a stale v7 row (file gone) gets a 200 + OOB toast so the failure is
        # actually visible, instead of a bare 404 htmx silently drops for this target.
        if is_v7:
            return _tagwrite_stale_toast_response(request, "File not found -- it may have been removed or already processed.")
        return HTMLResponse(content="File not found", status_code=404)

    if not await is_applied(session, file_id):
        # phaze-nvll defect 3: file still exists (a stale row -- the execution was reverted since
        # render), so redraw it unchanged (still pending) alongside the toast rather than dropping it.
        if is_v7:
            row_context = await _tagwrite_row_context(session, file_record, row_state="pending")
            return _tagwrite_diff_row_response(request, row_context, "Only executed files can have tags written.")
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

    if is_v7:
        # phaze-nvll defects 1+2: the v7 row gets the shared _diff_row.html back, in "approved" (WITH
        # a working UNDO) for a real write outcome (COMPLETED/DISCREPANCY), or "pending" (APPROVE
        # still available to retry) when nothing was actually written (FAILED / a raised ValueError).
        row_state = "approved" if status in (TagWriteStatus.COMPLETED, TagWriteStatus.DISCREPANCY) else "pending"
        row_context = await _tagwrite_row_context(session, file_record, row_state=row_state)
        return _tagwrite_diff_row_response(request, row_context, toast_message)

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
    """REVIEW-02 (D-03 / OQ-1): write tags for every qualifying applied file, server-re-queried.

    Mirrors ``tracklists.reject_low_confidence`` discipline -- the server re-queries the candidate
    set at submit (applied files -- an executed proposal exists, READ-05/D-01 -- with metadata that
    have NO COMPLETED ``TagWriteLog``) and applies the LOCKED D-03 / OQ-1 predicate
    (:func:`_qualifies_for_bulk_write`): ``>= 1`` changed field AND no field that would blank an
    existing tag. It reads NO client-supplied id-list, so a stale or forged selection can never
    mass-apply. Non-qualifying files stay per-file Approve/Edit/Skip. The candidate set is capped at
    :data:`_MAX_BULK_TAG_WRITE` per submit (D-03) so a large first-time-visible applied backlog cannot
    blow up the loop. Each qualifying file is written via the EXISTING :func:`execute_tag_write`.
    """
    terminal_subq = _terminal_tagwrite_subq()
    stmt = (
        select(FileRecord)
        .options(selectinload(FileRecord.file_metadata))
        .where(applied_clause(), FileRecord.id.not_in(terminal_subq))
        .order_by(FileRecord.original_filename)
        .limit(_MAX_BULK_TAG_WRITE)  # D-03: bound the operator-triggered loop at 200K scale
    )
    file_records = list((await session.execute(stmt)).scalars().all())

    written = 0
    for fr in file_records:
        tracklist = await _get_tracklist_for_file(session, fr.id)
        discogs_link = await _get_accepted_discogs_link(session, fr.id)
        proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename, discogs_link=discogs_link)
        comparison = _build_comparison(fr.file_metadata, proposed)
        if _count_changes(comparison) < 1:
            # WR-01: a zero-change applied file has nothing to write. Persist a terminal NO_OP marker
            # so ``_terminal_tagwrite_subq`` EVICTS it -- otherwise it re-occupies this same window on
            # every submit and permanently starves the qualifying files behind it. This is the write
            # path (it already commits below); the read-only render builder never writes.
            session.add(
                TagWriteLog(
                    file_id=fr.id,
                    before_tags={},
                    after_tags={},
                    source="bulk_noop",
                    status=TagWriteStatus.NO_OP.value,
                )
            )
            continue
        if not _qualifies_for_bulk_write(comparison):
            # A >=1-change file that would blank an existing tag: never bulk-written (stays per-file
            # Approve/Edit/Skip). ``compute_proposed_tags`` never blanks, so this is a defensive path.
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
    is_v7 = _is_v7_tagwrite_target(request, file_id)

    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        # phaze-nvll defect 3: a stale v7 row (file gone) gets a 200 + OOB toast, not a bare 404.
        if is_v7:
            return _tagwrite_stale_toast_response(request, "File not found -- it may have been removed or already processed.")
        return HTMLResponse(content="File not found", status_code=404)

    latest = await _get_latest_write_log(session, file_id)
    if latest is None:
        # phaze-nvll defect 3: nothing to undo (a race/stale row) -- redraw the row as pending
        # alongside the toast rather than silently doing nothing.
        if is_v7:
            row_context = await _tagwrite_row_context(session, file_record, row_state="pending")
            return _tagwrite_diff_row_response(request, row_context, "No prior tag write to undo.")
        return HTMLResponse(content="No prior tag write to undo", status_code=404)

    log_entry = await execute_tag_write(session, file_record, latest.before_tags, source="undo")
    await session.commit()

    if is_v7:
        # phaze-nvll: undo restores the row -- back to "pending" (APPROVE available again) once the
        # reversal write actually completed; a failed reversal keeps "approved" (UNDO stays available
        # to retry) rather than claiming a revert that did not happen.
        row_state = "pending" if log_entry.status == TagWriteStatus.COMPLETED else "approved"
        row_context = await _tagwrite_row_context(session, file_record, row_state=row_state)
        return _tagwrite_diff_row_response(request, row_context, f"Reverted tags for {file_record.original_filename}.")

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
