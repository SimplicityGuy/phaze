"""Tag review UI router -- per-file / bulk tag writing, undo, and a legacy bookmark redirect.

phaze-y4s6: the standalone tag review list + comparison page (``GET /tags/`` fragment branch,
``tags/partials/tag_list.html``) and its per-row expand-into inline-edit/compare surface
(``tag_comparison.html``/``inline_edit.html``/``inline_display.html``/``tag_row.html``) had no live
caller left post-v7-cutover -- the live tagwrite workspace
(``pipeline/partials/tagwrite_workspace.html``) renders its queue through the shared
``_diff_row.html`` and explicitly ships no inline-edit or comparison surface. All of it was deleted
outright; ``GET /tags/`` now only resolves the legacy bookmark into the shell (SHELL-05), and
``write_file_tags``/``undo_tag_write`` always return the v7 ``_diff_row.html`` shape.
"""

from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from phaze.database import get_session
from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack
from phaze.services.stage_status import applied_clause, is_applied
from phaze.services.tag_proposal import CORE_FIELDS, compute_proposed_tags
from phaze.services.tag_writer import execute_tag_write


logger = structlog.get_logger(__name__)

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


# phaze-u28m: a fixed application-defined key for the bulk-tag-write Postgres advisory lock. It
# serializes the whole ``bulk_write_no_discrepancies`` operation across requests so a duplicate or
# concurrent submit cannot re-select the same still-non-terminal candidate set and double-write tags
# on disk / append duplicate audit rows (the TOCTOU window the deferred single commit used to leave
# open). SESSION-scoped (``pg_(try_)advisory_lock``), NOT xact-scoped: phaze-k7g6 makes the loop
# commit per file, which would release a ``pg_advisory_xact_lock`` after the first file and reopen
# the race. Arbitrary stable 63-bit constant (ASCII "phazetag" folded).
_BULK_TAGWRITE_LOCK_KEY = 0x506861_7A657461


async def _acquire_bulk_tagwrite_lock(session: AsyncSession) -> bool:
    """Try to take the session-scoped bulk-tag-write advisory lock. ``True`` if acquired."""
    result = await session.execute(select(func.pg_try_advisory_lock(_BULK_TAGWRITE_LOCK_KEY)))
    return bool(result.scalar())


async def _release_bulk_tagwrite_lock(session: AsyncSession) -> None:
    """Release the session-scoped bulk-tag-write advisory lock (idempotent-safe on our own hold)."""
    await session.execute(select(func.pg_advisory_unlock(_BULK_TAGWRITE_LOCK_KEY)))


async def _has_terminal_tagwrite(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Re-check under the lock whether ``file_id`` already carries a terminal TagWriteLog.

    phaze-u28m: guards the interleaving with the per-file route (which the bulk advisory lock does
    NOT block) -- a file that gained a COMPLETED/NO_OP log between the candidate SELECT and its turn
    in the loop must be skipped rather than written a second time.
    """
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id, TagWriteLog.status.in_(_TERMINAL_TAGWRITE_STATUSES))
    return bool((await session.execute(stmt)).scalar())


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
        # phaze-evn9: confidence is non-unique, so a tie left the pick arbitrary and unstable
        # across queries. ``id`` tiebreaks equal confidence deterministically, mirroring the
        # ``_get_latest_write_log`` / ``_get_write_log_to_undo`` pattern above.
        .order_by(DiscogsLink.confidence.desc(), DiscogsLink.id.desc())
        .limit(1)
    )
    link_result = await session.execute(link_stmt)
    return link_result.scalar_one_or_none()


async def _get_latest_write_log(session: AsyncSession, file_id: uuid.UUID) -> TagWriteLog | None:
    """Get the most recent TagWriteLog for a file (any status/source), for status display."""
    stmt = (
        select(TagWriteLog)
        .where(TagWriteLog.file_id == file_id)
        # ``id`` tiebreaks equal ``written_at`` (server ``now()`` is per-transaction) so the "latest"
        # row is deterministic.
        .order_by(TagWriteLog.written_at.desc(), TagWriteLog.id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# phaze-soph: statuses whose TagWriteLog actually mutated the file on disk and can therefore be
# reverted. COMPLETED and DISCREPANCY both ran ``write_tags`` (DISCREPANCY = wrote, but verify found a
# normalization mismatch), and VERIFY_FAILED (phaze-vq3g) also LANDED on disk -- only its confirming
# re-read failed -- so all three carry a real before_tags snapshot to restore. FAILED never wrote and
# NO_OP is a zero-change marker, so neither is a real write to undo.
_UNDOABLE_TAGWRITE_STATUSES = (TagWriteStatus.COMPLETED, TagWriteStatus.DISCREPANCY, TagWriteStatus.VERIFY_FAILED)


async def _get_write_log_to_undo(session: AsyncSession, file_id: uuid.UUID) -> TagWriteLog | None:
    """Get the latest TagWriteLog that is the ACTUAL write an undo should revert.

    phaze-soph: ``_get_latest_write_log`` returns the newest row regardless of status/source, so a
    FAILED retry (before_tags = the post-previous-write disk state) or a bulk NO_OP marker
    (before_tags = {}) shadows the real write, and undo re-applies the wrong snapshot while toasting
    'Reverted'. This selects the newest row that truly wrote to disk (COMPLETED/DISCREPANCY) and is
    not itself a reversal (``source != 'undo'``) -- the row whose ``before_tags`` restores the
    pre-write state.
    """
    stmt = (
        select(TagWriteLog)
        .where(
            TagWriteLog.file_id == file_id,
            TagWriteLog.status.in_(_UNDOABLE_TAGWRITE_STATUSES),
            TagWriteLog.source != "undo",
        )
        .order_by(TagWriteLog.written_at.desc(), TagWriteLog.id.desc())
        .limit(1)
    )
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
# non-2xx bodies by default; shell.html only special-cases #record-body).
#
# phaze-y4s6: the legacy tag list/comparison pages (tag_list.html, tag_comparison.html) that used
# to target `#row-{file_id}` and require the opt-in HX-Target negotiation below had no live caller
# left post-v7-cutover and were deleted outright (along with tag_row.html itself), so both routes
# now always return the v7 _diff_row.html response -- there is no other shape left to negotiate.
_V7_TAGWRITE_ROW_PREFIX = "tagwrite-row"


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


@router.get("/", response_class=RedirectResponse)
async def list_tags() -> RedirectResponse:
    """SHELL-05 (D-03): resolve a legacy ``/tags/`` bookmark into the v7.0 shell.

    phaze-y4s6: this used to also serve an in-page HX-filtered/paginated/sorted table (rendering
    ``tags/partials/tag_list.html``, its per-row expand-into ``tag_comparison.html`` inline-edit
    comparison panel, and the ``edit_tag_field``/``save_tag_field`` inline-edit fragments it alone
    offered). The live v7.0 tagwrite workspace (``pipeline/partials/tagwrite_workspace.html``)
    renders its own queue via the shared ``_diff_row.html`` and explicitly ships NO inline-edit
    ("Tag inline-edit is OUT of the initial cut") and no comparison page -- there was no live
    caller left to preserve any of that surface for, so it and the ``TAGS_SORT`` contract that fed
    it were deleted outright.
    """
    return RedirectResponse(url="/s/tagwrite", status_code=302)


@router.post("/{file_id}/write", response_class=HTMLResponse)
async def write_file_tags(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Execute tag write for a file using form-submitted proposed values.

    phaze-y4s6: this used to fork on ``_is_v7_tagwrite_target`` (the shared ``_diff_row.html``
    workspace response vs a legacy ``tag_row.html`` fallback for the non-v7 tag review list). That
    legacy list (``tags/partials/tag_list.html``/``tag_comparison.html``, the only surface that ever
    POSTed here without the v7 ``HX-Target``) had no live caller left post-v7-cutover and was
    deleted outright, along with ``tag_row.html`` (its sole other includer). The v7 response shape
    is therefore now the ONLY shape.
    """
    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        # phaze-nvll defect 3: a stale row (file gone) gets a 200 + OOB toast so the failure is
        # actually visible, instead of a bare 404 htmx silently drops for this target.
        return _tagwrite_stale_toast_response(request, "File not found -- it may have been removed or already processed.")

    if not await is_applied(session, file_id):
        # phaze-nvll defect 3: file still exists (a stale row -- the execution was reverted since
        # render), so redraw it unchanged (still pending) alongside the toast rather than dropping it.
        row_context = await _tagwrite_row_context(session, file_record, row_state="pending")
        return _tagwrite_diff_row_response(request, row_context, "Only executed files can have tags written.")

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
        elif status == TagWriteStatus.VERIFY_FAILED:
            # phaze-vq3g: the write LANDED but the immediate verify re-read failed (transient I/O).
            # Do not claim a discrepancy -- the on-disk tags are the ones sent; the file just could
            # not be confirmed. It resurfaces for a later re-verify that self-heals to COMPLETED.
            toast_message = f"Tags written to {file_record.original_filename}, but the file could not be re-read to verify ({log_entry.error_message or 'verify failed'}). The write itself succeeded; it will re-verify later."
        else:
            toast_message = f"Tag write failed: {log_entry.error_message or 'Unknown error'}. The file may be read-only or corrupted. Check file permissions and try again."
    except ValueError as exc:
        status = "failed"
        toast_message = f"Tag write failed: {exc}"

    # phaze-nvll defects 1+2: the row gets the shared _diff_row.html back, in "approved" (WITH a
    # working UNDO) for a write that LANDED on disk (COMPLETED/DISCREPANCY/VERIFY_FAILED -- all
    # mutated the file), or "pending" (APPROVE still available to retry) when nothing was actually
    # written (FAILED / a raised ValueError).
    row_state = "approved" if status in (TagWriteStatus.COMPLETED, TagWriteStatus.DISCREPANCY, TagWriteStatus.VERIFY_FAILED) else "pending"
    row_context = await _tagwrite_row_context(session, file_record, row_state=row_state)
    return _tagwrite_diff_row_response(request, row_context, toast_message)


def _bulk_write_toast(written: int, discrepancy: int, verify_failed: int, failed: int) -> str:
    """Build a truthful bulk-write toast (phaze-5j82).

    Only ``written`` (COMPLETED) files are reported as tagged. DISCREPANCY, VERIFY_FAILED, and
    FAILED outcomes are surfaced separately so the operator is never told "N files tagged" when
    zero tags actually landed.
    """
    if not (written or discrepancy or verify_failed or failed):
        return "Nothing matched -- no executed files qualify for a no-discrepancy bulk write right now."

    parts = [f"{written} file{'s' if written != 1 else ''} tagged"]
    extras: list[str] = []
    if discrepancy:
        extras.append(f"{discrepancy} with discrepancies")
    if verify_failed:
        extras.append(f"{verify_failed} written but unverified")
    if failed:
        extras.append(f"{failed} failed")
    if extras:
        return f"{parts[0]}; {', '.join(extras)}. Review the audit log for the non-clean writes."
    return f"{parts[0]} (no discrepancies)."


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

    phaze-gwe1: the pending workspace's rows are NOT re-queried/re-rendered by anything else after
    this commits (no self-poll, and the chrome ``/pipeline/stats`` poll is counts-only) -- every row
    this handler resolves to a TERMINAL outcome (a fresh NO_OP marker, or a write that actually
    COMPLETED) is stale on screen: still "pending" with a live APPROVE that would re-write an
    already-written file and shadow the bulk write's own before/after snapshot in the undo chain. The
    response therefore also OOB-removes exactly those rows (keyed by ``tagwrite-row-{id}``, the SAME
    id the workspace renders) and refreshes the subcount. A DISCREPANCY or FAILED outcome is
    deliberately left in place (both are non-terminal by design -- DISCREPANCY re-offers itself for a
    retry, FAILED never wrote anything -- so the row staying pending is correct, not stale).
    """
    # phaze-u28m: serialize the whole bulk operation. A second concurrent/duplicate submit that
    # cannot take the lock does NOTHING (no re-select, no double disk write, no duplicate audit rows)
    # rather than racing the first. Fail-fast (``pg_try_advisory_lock``) suits a single-user tool: a
    # double-click gets a clear "already in progress" toast instead of silently re-tagging every file.
    if not await _acquire_bulk_tagwrite_lock(session):
        stats = await _get_tag_stats(session)
        return templates.TemplateResponse(
            request=request,
            name="tags/partials/bulk_write_response.html",
            context={
                "request": request,
                "stats": stats,
                "written": 0,
                "toast_message": "A bulk tag write is already in progress -- nothing was re-written. Wait for it to finish, then retry.",
            },
        )

    written = 0
    failed = 0
    discrepancy = 0
    verify_failed = 0
    # phaze-gwe1: files whose tag-write reached a TERMINAL state this pass (COMPLETED / NO_OP) --
    # the response removes their stale pending rows. DISCREPANCY/VERIFY_FAILED/FAILED are
    # non-terminal by design and stay in the queue.
    resolved_ids: list[uuid.UUID] = []
    try:
        terminal_subq = _terminal_tagwrite_subq()
        stmt = (
            select(FileRecord)
            .options(selectinload(FileRecord.file_metadata))
            .where(applied_clause(), FileRecord.id.not_in(terminal_subq))
            .order_by(FileRecord.original_filename)
            .limit(_MAX_BULK_TAG_WRITE)  # D-03: bound the operator-triggered loop at 200K scale
        )
        file_records = list((await session.execute(stmt)).scalars().all())

        for fr in file_records:
            # Capture the id BEFORE any write: a per-file rollback (below) expires the ORM instance,
            # so a later ``fr.id`` access would trigger a lazy reload (async IO) from a sync context.
            file_id = fr.id
            # phaze-k7g6: isolate each file. A single bad file (e.g. a ValueError from a concurrently
            # un-applied file, or a transient read error) must SKIP -- never abort the batch and never
            # discard the already-committed audit rows of prior files.
            try:
                # phaze-u28m: re-check terminal status under the lock. The advisory lock blocks a
                # concurrent BULK submit, but a per-file write_file_tags could have landed a terminal
                # log for this candidate since the SELECT -- skip it rather than write it twice.
                if await _has_terminal_tagwrite(session, file_id):
                    continue

                tracklist = await _get_tracklist_for_file(session, file_id)
                discogs_link = await _get_accepted_discogs_link(session, file_id)
                proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename, discogs_link=discogs_link)
                comparison = _build_comparison(fr.file_metadata, proposed)
                if _count_changes(comparison) < 1:
                    # WR-01: a zero-change applied file has nothing to write. Persist a terminal NO_OP
                    # marker so ``_terminal_tagwrite_subq`` EVICTS it -- otherwise it re-occupies this
                    # same window on every submit and permanently starves the qualifying files behind it.
                    session.add(
                        TagWriteLog(
                            file_id=file_id,
                            before_tags={},
                            after_tags={},
                            source="bulk_noop",
                            status=TagWriteStatus.NO_OP.value,
                        )
                    )
                    # phaze-k7g6: commit the marker immediately so a later abort cannot lose it.
                    await session.commit()
                    resolved_ids.append(file_id)  # phaze-gwe1: now terminal -- remove the stale pending row
                    continue
                if not _qualifies_for_bulk_write(comparison):
                    # A >=1-change file that would blank an existing tag: never bulk-written (stays
                    # per-file Approve/Edit/Skip). ``compute_proposed_tags`` never blanks, so defensive.
                    continue
                tags: dict[str, str | int | None] = {k: v for k, v in proposed.items() if v is not None}
                log_entry = await execute_tag_write(session, fr, tags, source="proposal")
                # phaze-k7g6: commit the audit row atomically with the disk mutation it describes, so a
                # mid-loop cancellation/crash can never leave a written file without its TagWriteLog
                # (which holds the before_tags UNDO snapshot).
                await session.commit()

                # phaze-5j82: count outcomes truthfully -- only a real COMPLETED write is a success.
                # FAILED (nothing written) and DISCREPANCY/VERIFY_FAILED (written but not confirmed
                # clean) are tallied separately and surfaced, never reported as clean successes.
                if log_entry.status == TagWriteStatus.COMPLETED:
                    written += 1
                    resolved_ids.append(file_id)  # phaze-gwe1: terminal clean write -- remove the stale pending row
                elif log_entry.status == TagWriteStatus.DISCREPANCY:
                    discrepancy += 1
                elif log_entry.status == TagWriteStatus.VERIFY_FAILED:
                    verify_failed += 1
                else:
                    failed += 1
            except Exception:
                # phaze-k7g6: roll back only this file's uncommitted work (prior per-file commits
                # stand) and keep going. A raised ValueError/DB error is a failed file, not a batch abort.
                await session.rollback()
                failed += 1
                logger.warning("bulk_tag_write_file_skipped", file_id=str(file_id), exc_info=True)
                continue
    finally:
        await _release_bulk_tagwrite_lock(session)
        await session.commit()

    stats = await _get_tag_stats(session)
    toast_message = _bulk_write_toast(written, discrepancy, verify_failed, failed)
    # phaze-gwe1: re-query the SAME builder the workspace itself renders from (deferred import --
    # services.review imports helpers FROM this module, so importing it back at module scope would
    # cycle; by call time this module is already fully loaded) so the refreshed subcount always
    # matches the row count the operator actually sees after this OOB update lands.
    from phaze.services.review import get_tagwrite_review_rows  # noqa: PLC0415 -- deferred to break the tags<->review import cycle

    remaining = len(await get_tagwrite_review_rows(session))
    subcount = f"{remaining} awaiting approval · mutagen will write these tags"
    return templates.TemplateResponse(
        request=request,
        name="tags/partials/bulk_write_response.html",
        context={
            "request": request,
            "stats": stats,
            "written": written,
            "toast_message": toast_message,
            "resolved_ids": resolved_ids,
            "subcount": subcount,
            "row_id_prefix": _V7_TAGWRITE_ROW_PREFIX,
        },
    )


@router.post("/{file_id}/undo", response_class=HTMLResponse)
async def undo_tag_write(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-05 (D-04): revert a tag write by re-applying ``TagWriteLog.before_tags``.

    Reuses the EXISTING :func:`execute_tag_write` mutagen path (``source="undo"``) to restore the
    snapshot captured before the latest write -- NO new apply/undo logic. Appends one further
    ``TagWriteLog`` so the append-only audit trail stays coherent (REVIEW-05: every apply,
    including a reversal, is one audit row).

    phaze-y4s6: this used to fork on ``_is_v7_tagwrite_target`` the same way ``write_file_tags``
    did; the legacy ``tag_row.html`` fallback it shared with that route is gone (see
    ``write_file_tags``'s docstring), so the v7 ``_diff_row.html`` response is now the ONLY shape.
    """
    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        # phaze-nvll defect 3: a stale row (file gone) gets a 200 + OOB toast, not a bare 404.
        return _tagwrite_stale_toast_response(request, "File not found -- it may have been removed or already processed.")

    # phaze-soph: target the latest row that ACTUALLY wrote to disk (COMPLETED/DISCREPANCY, not a
    # prior undo), skipping FAILED/NO_OP shadows whose before_tags would restore the wrong state.
    latest = await _get_write_log_to_undo(session, file_id)
    if latest is None:
        # phaze-nvll defect 3: nothing to undo (a race/stale row) -- redraw the row as pending
        # alongside the toast rather than silently doing nothing.
        row_context = await _tagwrite_row_context(session, file_record, row_state="pending")
        return _tagwrite_diff_row_response(request, row_context, "No prior tag write to undo.")

    # phaze-04bz: undo must be idempotent. If the most recent operation on this file was already a
    # COMPLETED reversal (an htmx double-click, or a second tab firing the still-rendered UNDO), a
    # repeat undo must be a NO-OP -- never a re-apply of the written tags -- with an honest toast.
    newest = await _get_latest_write_log(session, file_id)
    if newest is not None and newest.source == "undo" and newest.status == TagWriteStatus.COMPLETED:
        already_message = f"Tags for {file_record.original_filename} were already reverted."
        row_context = await _tagwrite_row_context(session, file_record, row_state="pending")
        return _tagwrite_diff_row_response(request, row_context, already_message)

    log_entry = await execute_tag_write(session, file_record, latest.before_tags, source="undo")
    await session.commit()

    # phaze-26t7: the toast must reflect the REAL on-disk outcome. execute_tag_write swallows
    # mutagen/file errors into a FAILED log rather than raising, so an unconditional 'Reverted tags'
    # lies whenever the reversal write did not land. Branch the message on status, mirroring
    # write_file_tags: success only for COMPLETED, a distinct note for DISCREPANCY, and the error for
    # FAILED.
    filename = file_record.original_filename
    if log_entry.status == TagWriteStatus.COMPLETED:
        toast_message = f"Reverted tags for {filename}."
    elif log_entry.status == TagWriteStatus.DISCREPANCY:
        disc_count = len(log_entry.discrepancies) if log_entry.discrepancies else 0
        toast_message = (
            f"Reverted tags for {filename} with {disc_count} discrepancy. Re-read values differ from "
            "what was restored -- usually encoding normalization. Review the audit log for details."
        )
    else:
        toast_message = (
            f"Undo failed for {filename}: {log_entry.error_message or 'Unknown error'}. The file may be "
            "read-only or corrupted. Check file permissions and try again."
        )

    # phaze-nvll: undo restores the row -- back to "pending" (APPROVE available again) once the
    # reversal write actually completed; a failed reversal keeps "approved" (UNDO stays available
    # to retry) rather than claiming a revert that did not happen.
    row_state = "pending" if log_entry.status == TagWriteStatus.COMPLETED else "approved"
    row_context = await _tagwrite_row_context(session, file_record, row_state=row_state)
    return _tagwrite_diff_row_response(request, row_context, toast_message)
