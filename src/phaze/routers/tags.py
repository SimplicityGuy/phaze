"""Tag review UI router -- per-file / bulk tag writing, undo, and a legacy bookmark redirect.

phaze-y4s6: the standalone tag review list + comparison page (``GET /tags/`` fragment branch,
``tags/partials/tag_list.html``) and its per-row expand-into inline-edit/compare surface
(``tag_comparison.html``/``inline_edit.html``/``inline_display.html``/``tag_row.html``) had no live
caller left post-v7-cutover -- the then-live tagwrite workspace rendered its queue through the
shared ``_diff_row.html`` and explicitly shipped no inline-edit or comparison surface. All of it was
deleted outright; ``GET /tags/`` now only resolves the legacy bookmark into the shell (SHELL-05), and
``write_file_tags``/``undo_tag_write`` always return the v7 ``_diff_row.html`` shape.

phaze-tzy6s.11 / ADR-0008: the standalone tagwrite workspace
(``pipeline/partials/tagwrite_workspace.html``) is itself gone now -- tag decisions were consolidated
into Changes Review, whose "Tag Changes" section (``pipeline/partials/_changes_list.html``, the
``tagwrite-row`` block) is the only live renderer of this router's rows and the only UI surface that
authorizes a tag write. ``/s/tagwrite`` survives as a compatibility alias onto Changes Review, not as
a separate approval surface.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal, cast
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import StringConstraints
from sqlalchemy import exists, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from phaze.database import get_session
from phaze.models.file import FileRecord
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services.stage_status import applied_clause, is_applied
from phaze.services.tag_comparison import (
    _build_comparison,
    _count_changes,
    _encode_tag_review_token,
    _get_accepted_discogs_link,
    _get_tracklist_for_file,
    _qualifies_for_bulk_write,
    _summarize_tags,
    _tag_review_payload,
    _terminal_tagwrite_subq,
)
from phaze.services.tag_proposal import CORE_FIELDS, compute_proposed_tags
from phaze.services.tag_writer import TagWriteAlreadyQueuedError, enqueue_tag_write


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/tags", tags=["tags"])

# D-03: bound the operator-triggered no-discrepancy bulk loop. Reviving the applied() gate can make a
# large first-time-visible applied backlog suddenly enumerable; cap one submit at a batch of this size
# (low-thousands, consistent with in-tree page bounds) so the loop cannot blow up at 200K scale.
_MAX_BULK_TAG_WRITE = 2000
TagReviewTokenWire = Annotated[str, StringConstraints(max_length=131_072)]

# WR-01: statuses that make an applied file TERMINAL for the tag-write queue -- it has either been
# written (COMPLETED) or determined to need no write (NO_OP). Both are excluded from the candidate
# window so neither can re-occupy the alphabetically-first ``.limit()`` slots and starve qualifying
# files. DISCREPANCY is intentionally NOT terminal (the file re-appears so the operator can retry).
#
# phaze-vwyco: kept as documentation of the two terminal STATUS values, but the actual anti-join
# (:func:`_terminal_tagwrite_subq`) no longer keys off this tuple alone -- a bare ``status IN (...)``
# also matches a completed UNDO (``source="undo"``, status COMPLETED), which must NOT be terminal.
_TERMINAL_TAGWRITE_STATUSES = (TagWriteStatus.COMPLETED, TagWriteStatus.NO_OP)

# phaze-b4u3p: the predicate itself (``_terminal_tagwrite_subq``) moved to
# ``services/tag_comparison.py`` -- see that module's docstring for why -- and is re-imported
# above under the same name so every reference in this file keeps working unchanged.

# phaze-u28m: a fixed application-defined key for the bulk-tag-write Postgres advisory lock. It
# serializes the whole ``bulk_write_no_discrepancies`` operation across requests so a duplicate or
# concurrent submit cannot re-select the same still-non-terminal candidate set and double-write tags
# on disk / append duplicate audit rows (the TOCTOU window the deferred single commit used to leave
# open). SESSION-scoped (``pg_(try_)advisory_lock``), NOT xact-scoped: phaze-k7g6 makes the loop
# commit per file, which would release a ``pg_advisory_xact_lock`` after the first file and reopen
# the race. Arbitrary stable 63-bit constant (ASCII "phazetag" folded).
_BULK_TAGWRITE_LOCK_KEY = 0x506861_7A657461


async def _acquire_bulk_tagwrite_lock(conn: AsyncConnection) -> bool:
    """Try to take the session-scoped bulk-tag-write advisory lock on a PINNED connection.

    ``True`` if acquired. phaze-yhhy: this MUST run on a connection the caller holds open for the
    whole operation -- see :func:`_bulk_tagwrite_lock_connection`.
    """
    result = await conn.execute(select(func.pg_try_advisory_lock(_BULK_TAGWRITE_LOCK_KEY)))
    return bool(result.scalar())


async def _release_bulk_tagwrite_lock(conn: AsyncConnection) -> None:
    """Release the session-scoped bulk-tag-write advisory lock on the SAME pinned connection.

    Idempotent-safe on our own hold. phaze-yhhy: must be the identical ``AsyncConnection`` object
    :func:`_acquire_bulk_tagwrite_lock` was called on -- a Postgres session-scoped advisory lock is
    bound to the physical DBAPI connection, not to any ORM transaction.
    """
    await conn.execute(select(func.pg_advisory_unlock(_BULK_TAGWRITE_LOCK_KEY)))


async def _bulk_tagwrite_lock_connection(session: AsyncSession) -> tuple[AsyncConnection, bool]:
    """Resolve the connection to acquire/release the bulk-tag-write advisory lock on.

    phaze-yhhy: ``bulk_write_no_discrepancies`` commits per file (phaze-k7g6), and a pooled
    ``AsyncSession`` returns its DBAPI connection to the pool on every ``commit()``/``rollback()``.
    Acquiring the SESSION-scoped lock on ``session`` and releasing it on ``session`` again therefore
    acquires and releases on WHATEVER connection the pool happens to hand back each time -- almost
    always a DIFFERENT physical connection than the one that took the lock once the pool holds more
    than one idle connection, silently no-op'ing the release and leaking the lock onto an idle pooled
    connection (unrecoverable short of that connection's ``pool_recycle``/next ping-death).

    The fix is to open a connection OUTSIDE the ORM session and hold it for the caller's entire
    request, immune to how many times ``session`` itself commits or rolls back in between. Returns
    ``(connection, owns_it)`` -- the caller must ``await connection.close()`` iff ``owns_it`` is
    ``True``.

    ``session.bind`` is the production ``AsyncEngine`` under normal operation, so a genuinely new
    connection is opened. Under the test suite's hermetic ``session`` fixture every DB-bound fixture
    is deliberately pinned to ONE shared ``AsyncConnection`` for the whole test (so seeded rows stay
    visible across the request) -- opening yet another connection there would just be a second,
    functionally-redundant physical connection to the same DB, so this reuses the existing one
    instead of fighting that harness.
    """
    bind = session.bind
    if isinstance(bind, AsyncConnection):
        return bind, False
    conn = await bind.connect()
    return conn, True


async def _has_terminal_tagwrite(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Re-check under the lock whether ``file_id`` already carries a terminal TagWriteLog.

    phaze-u28m: guards the interleaving with the per-file route (which the bulk advisory lock does
    NOT block) -- a file that gained a COMPLETED/NO_OP log between the candidate SELECT and its turn
    in the loop must be skipped rather than written a second time.

    phaze-vwyco: this used to run its own bare ``status IN (...)`` check, which -- like the
    candidate-window anti-join it re-verifies -- treated a completed undo as terminal. Reuses
    :func:`_terminal_tagwrite_subq`'s chain-aware predicate (scoped to this one ``file_id``) so both
    checks agree: a file the anti-join now correctly re-admits is never re-evicted here. Uses the
    standalone ``exists(...)`` construct, not the ``Select.exists()`` method -- see the identical
    phaze-9dwb note in :func:`_terminal_tagwrite_subq`.
    """
    stmt = select(exists(_terminal_tagwrite_subq(file_id)))
    return bool((await session.execute(stmt)).scalar())


async def _has_queued_tagwrite(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Re-check under the lock whether ``file_id`` already has an unresolved ``queued`` write.

    phaze-lwqk: ``queued`` is deliberately NON-terminal (see ``TagWriteStatus.QUEUED``'s
    docstring), so the candidate SELECT above does not exclude it -- a per-file
    ``write_file_tags``/``undo`` could have dispatched a job for this file since the SELECT. Skip
    it here rather than let ``enqueue_tag_write`` raise ``TagWriteAlreadyQueuedError`` for it (which
    would still correctly refuse the second dispatch, just via a spurious "failed" tally instead of
    a clean skip).
    """
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id, TagWriteLog.status == TagWriteStatus.QUEUED.value)
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


# phaze-b4u3p: ``_get_tracklist_for_file``, ``_get_accepted_discogs_link`` and their batch forms
# (``_get_tracklists_for_files``, ``_get_accepted_discogs_links_for_files``) moved to
# ``services/tag_comparison.py`` and are re-imported above under the same names.


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
    """Get the HEAD of the current write chain -- the TagWriteLog an undo should revert to.

    phaze-soph: ``_get_latest_write_log`` returns the newest row regardless of status/source, so a
    FAILED retry (before_tags = the post-previous-write disk state) or a bulk NO_OP marker
    (before_tags = {}) shadowed the real write, and undo re-applied the wrong snapshot while
    toasting 'Reverted'. Excluding those non-``_UNDOABLE_TAGWRITE_STATUSES`` rows fixed that case.

    phaze-lwqk: a SUCCESSFUL retry is still a shadow. DISCREPANCY is deliberately non-terminal
    (``_TERMINAL_TAGWRITE_STATUSES`` -- the row "re-offers itself for a retry"), so an ordinary,
    non-racy operator sequence -- APPROVE -> DISCREPANCY -> APPROVE again -> UNDO -- appends a
    SECOND undoable row whose ``before_tags`` is the disk state the FIRST write already left
    (not the true original). Picking "newest" (phaze-soph's fix) still replays that shadow instead
    of the real original. The correct target is the OLDEST undoable, non-undo row since the last
    COMPLETED undo -- the head of the CURRENT write chain, not the tail. A completed undo resets
    the chain (a later write after it starts a genuinely new one), so only rows after that boundary
    are candidates.
    """
    boundary_stmt = (
        select(TagWriteLog.written_at, TagWriteLog.id)
        .where(TagWriteLog.file_id == file_id, TagWriteLog.source == "undo", TagWriteLog.status == TagWriteStatus.COMPLETED)
        .order_by(TagWriteLog.written_at.desc(), TagWriteLog.id.desc())
        .limit(1)
    )
    boundary = (await session.execute(boundary_stmt)).one_or_none()

    stmt = select(TagWriteLog).where(
        TagWriteLog.file_id == file_id,
        TagWriteLog.status.in_(_UNDOABLE_TAGWRITE_STATUSES),
        TagWriteLog.source != "undo",
    )
    if boundary is not None:
        boundary_written_at, boundary_id = boundary
        stmt = stmt.where(tuple_(TagWriteLog.written_at, TagWriteLog.id) > tuple_(boundary_written_at, boundary_id))
    stmt = stmt.order_by(TagWriteLog.written_at.asc(), TagWriteLog.id.asc()).limit(1)

    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# phaze-b4u3p: ``_build_comparison``, ``_count_changes``, ``_qualifies_for_bulk_write``,
# ``_summarize_tags``, ``_tag_review_payload``, ``_encode_tag_review_token`` and ``FIELD_LABELS``
# moved to ``services/tag_comparison.py`` (re-imported above under the same names). Decoding a
# wire token into a 400/409 HTTPException stays here -- that's a router concern, not a service one.


def _decode_tag_review_token(token: str, file_id: uuid.UUID | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="Invalid reviewed tag payload") from exc
    if not isinstance(payload, dict) or (file_id is not None and payload.get("file_id") != str(file_id)):
        raise HTTPException(status_code=400, detail="Reviewed tag payload does not match the file")
    return payload


async def _validate_tag_review_token(
    session: AsyncSession,
    file_record: FileRecord,
    token: str,
) -> tuple[dict[str, Any], dict[str, str | int | None]]:
    reviewed = _decode_tag_review_token(token, file_record.id)
    tracklist = await _get_tracklist_for_file(session, file_record.id)
    discogs_link = await _get_accepted_discogs_link(session, file_record.id)
    proposed = compute_proposed_tags(file_record.file_metadata, tracklist, file_record.original_filename, discogs_link=discogs_link)
    current = _tag_review_payload(file_record, tracklist, discogs_link, proposed)
    if reviewed != current:
        raise HTTPException(status_code=409, detail="Tag metadata or enrichment changed after review; refresh before approving")
    after = reviewed.get("after")
    if not isinstance(after, dict):
        raise HTTPException(status_code=400, detail="Invalid reviewed tag payload")
    return reviewed, {field: cast("str | int | None", after.get(field)) for field in CORE_FIELDS}


# phaze-nvll: the tag-write queue renders rows from the shared pipeline/partials/_diff_row.html
# partial and hx-targets each row's own div. (phaze-tzy6s.11 / ADR-0008: that queue used to be the
# standalone tagwrite_workspace.html; it is now the "Tag Changes" section of Changes Review --
# pipeline/partials/_changes_list.html, the tagwrite-row block. The row-id contract is
# unchanged.) write_file_tags and
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
        "review_token": _encode_tag_review_token(_tag_review_payload(file_record, tracklist, discogs_link, proposed)),
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
    offered). The tagwrite workspace that superseded it rendered its own queue via the shared
    ``_diff_row.html`` and explicitly shipped NO inline-edit ("Tag inline-edit is OUT of the initial
    cut") and no comparison page -- there was no live caller left to preserve any of that surface
    for, so it and the ``TAGS_SORT`` contract that fed it were deleted outright.

    phaze-tzy6s.11 / ADR-0008: ``/s/tagwrite`` is itself now a compatibility alias rendering
    ``pipeline/partials/changes_workspace.html`` (shell.py's ``_STAGE_PARTIALS``), so this redirect
    lands the bookmark on Changes Review -- the one workspace that authorizes tag writes -- rather
    than on a separate tag approval surface.
    """
    return RedirectResponse(url="/s/tagwrite", status_code=302)


def _tagwrite_dispatch_toast(status: str, filename: str, agent_id: str, error_message: str | None) -> str:
    """Toast for a dispatched (or undispatchable) tag write -- phaze-6bkk.

    The old wording asserted an outcome the api process was in no position to know, and its failure
    advice ("The file may be read-only or corrupted. Check file permissions and try again.") was
    actively misdirecting: under DIST-01 the real and universal cause was that the archive is not
    mounted in the api container at all, so the operator was sent hunting for permissions on a
    directory that does not exist there. Both are fixed at the source now -- the write runs on the
    agent that owns the mount -- so the toast only claims what actually happened: the job was handed
    off, or it could not be.
    """
    if status == TagWriteStatus.QUEUED:
        return f"Tag write queued for {filename} on agent {agent_id}. The file server applies it; the row updates when the agent reports back."
    detail = error_message or "Unknown error"
    return (
        f"Tag write could not be dispatched for {filename}: {detail}. Check that agent {agent_id} is reachable and its worker is running, then retry."
    )


@router.post("/{file_id}/write", response_class=HTMLResponse)
async def write_file_tags(
    request: Request,
    file_id: uuid.UUID,
    review_token: TagReviewTokenWire | None = Form(default=None),
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

    if not review_token:
        raise HTTPException(status_code=400, detail="A reviewed tag payload is required")
    reviewed, tags = await _validate_tag_review_token(session, file_record, review_token)
    tag_payload: dict[str, str | int | list[str] | None] = {field: value for field, value in tags.items() if value is not None}

    try:
        log_entry = await enqueue_tag_write(
            session,
            request.app.state.task_router,
            file_record,
            tag_payload,
            "proposal",
            reviewed_before_tags=reviewed["before"],
            review_source_versions=reviewed["sources"],
        )
        await session.commit()
        status = log_entry.status
        toast_message = _tagwrite_dispatch_toast(status, file_record.original_filename, file_record.agent_id, log_entry.error_message)
    except ValueError as exc:
        status = TagWriteStatus.FAILED.value
        toast_message = f"Tag write failed: {exc}"
    except TagWriteAlreadyQueuedError:
        # phaze-lwqk: a write is already in flight for this file (a double-click, two tabs, or the
        # bulk loop just dispatched it) -- redraw the row as "queued" rather than dispatching a
        # second concurrent write_file_tags job against the same file.
        status = TagWriteStatus.QUEUED.value
        toast_message = f"A tag write is already queued for {file_record.original_filename}. Wait for it to finish, then retry if needed."

    # phaze-nvll defects 1+2 / phaze-6bkk: the row gets the shared _diff_row.html back, in "queued"
    # (in flight on the owning agent -- no UNDO yet, because nothing has touched the disk and the
    # before-tags snapshot an undo re-applies does not exist until the agent reports it), or
    # "pending" (APPROVE still available to retry) when the dispatch itself failed.
    row_state = "queued" if status == TagWriteStatus.QUEUED else "pending"
    row_context = await _tagwrite_row_context(session, file_record, row_state=row_state)
    return _tagwrite_diff_row_response(request, row_context, toast_message)


@dataclass(frozen=True, slots=True)
class _BulkCandidate:
    """Plain (non-ORM) snapshot of one bulk-write candidate file (phaze-o2ln).

    ``bulk_write_no_discrepancies`` used to iterate live ``FileRecord`` ORM instances and read
    ``fr.id`` / ``fr.original_filename`` / ``fr.file_metadata`` throughout the loop. A per-file
    ``session.rollback()`` (the ``except Exception`` branch below) expires EVERY object still in the
    session's identity map, not just the one that failed -- so a LATER iteration touching any of
    those attributes on the (now expired) ``fr`` triggered a lazy reload, which under the async
    engine raises ``MissingGreenlet`` (or, for attributes reached inside a broader ``try``, gets
    miscounted as a spurious ``failed`` outcome for a file nothing was ever attempted on). Capturing
    every needed field into a plain object OUTSIDE the identity map -- built right after the
    candidate SELECT, before any rollback can happen -- makes the rest of the loop immune: nothing
    here can ever be expired by a SQLAlchemy transaction event.

    Satisfies :class:`phaze.services.tag_writer.TagWriteTarget` (``id`` + ``agent_id`` +
    ``current_path``), so it can be passed to :func:`~phaze.services.tag_writer.enqueue_tag_write`
    directly in place of the live ``FileRecord``.
    """

    id: uuid.UUID
    agent_id: str
    original_filename: str
    current_path: str
    metadata: SimpleNamespace | None


def _snapshot_bulk_candidate(file_record: FileRecord) -> _BulkCandidate:
    """Build a :class:`_BulkCandidate` from a freshly-loaded ``FileRecord`` (phaze-o2ln).

    ``file_record.file_metadata`` is snapshotted into a plain ``SimpleNamespace`` carrying only
    :data:`CORE_FIELDS` -- the exact surface :func:`~phaze.services.tag_proposal.compute_proposed_tags`
    and :func:`_build_comparison` read via ``getattr(..., field, None)``, so the snapshot is a
    drop-in substitute for the live ``FileMetadata`` relationship in both call sites.
    """
    metadata = file_record.file_metadata
    snapshot = SimpleNamespace(**{field: getattr(metadata, field, None) for field in CORE_FIELDS}) if metadata is not None else None
    return _BulkCandidate(
        id=file_record.id,
        agent_id=file_record.agent_id,
        original_filename=file_record.original_filename,
        current_path=file_record.current_path,
        metadata=snapshot,
    )


def _bulk_write_toast(queued: int, noop: int, failed: int) -> str:
    """Build a truthful bulk-dispatch toast (phaze-5j82, re-truthed for phaze-6bkk).

    phaze-5j82 established the rule this function exists for: never report an outcome the server has
    not observed. phaze-6bkk moves the write itself onto the agent, so at response time the ONLY
    honest tallies are how many writes were handed off (``queued``), how many files needed no write
    at all (``noop`` -- a terminal NO_OP marker, decided entirely from DB state), and how many could
    not be dispatched (``failed``). The per-file COMPLETED / DISCREPANCY / VERIFY_FAILED split now
    arrives asynchronously through the agent callback and is read from the audit log, not guessed at
    here.
    """
    if not (queued or noop or failed):
        return "Nothing matched -- no executed files qualify for a no-discrepancy bulk write right now."

    parts = [f"{queued} tag write{'s' if queued != 1 else ''} queued on the file server"]
    extras: list[str] = []
    if noop:
        extras.append(f"{noop} already correct (nothing to write)")
    if failed:
        extras.append(f"{failed} could not be dispatched")
    if extras:
        return f"{parts[0]}; {', '.join(extras)}. Outcomes land in the audit log as each agent reports back."
    return f"{parts[0]}. Outcomes land in the audit log as each agent reports back."


def _parse_bulk_review_tokens(review_tokens: list[str]) -> dict[uuid.UUID, tuple[str, dict[str, Any]]]:
    """Decode the submitted review tokens into a ``file_id``-keyed map (phaze-oc06m: extracted).

    Pure move out of :func:`bulk_write_no_discrepancies` -- same 400 on an unparseable ``file_id``,
    same last-token-wins de-duplication via dict assignment. A later token for the same file
    overwriting an earlier one was already the behavior of the loop this replaces.
    """
    reviewed_by_id: dict[uuid.UUID, tuple[str, dict[str, Any]]] = {}
    for token in review_tokens:
        payload = _decode_tag_review_token(token)
        try:
            file_id = uuid.UUID(str(payload.get("file_id")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid reviewed tag payload") from exc
        reviewed_by_id[file_id] = (token, payload)
    return reviewed_by_id


async def _bulk_write_lock_busy_response(request: Request, session: AsyncSession) -> HTMLResponse:
    """The "already in progress" branch of the bulk-write lock (phaze-oc06m: extracted, no change)."""
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


async def _load_bulk_candidates(
    session: AsyncSession,
    reviewed_by_id: dict[uuid.UUID, tuple[str, dict[str, Any]]],
) -> tuple[list[_BulkCandidate], dict[uuid.UUID, tuple[dict[str, Any], dict[str, str | int | None]]]]:
    """Re-query + re-validate the bulk-write candidate set (phaze-oc06m: extracted, no change).

    Mirrors the pre-extraction body exactly: the anti-join SELECT bounded at
    :data:`_MAX_BULK_TAG_WRITE` (D-03), per-file :func:`_validate_tag_review_token`, then the
    phaze-o2ln snapshot into plain :class:`_BulkCandidate` objects outside the ORM identity map
    before any per-candidate rollback in the caller's loop can expire them.
    """
    terminal_subq = _terminal_tagwrite_subq()
    stmt = (
        select(FileRecord)
        .options(selectinload(FileRecord.file_metadata))
        .where(applied_clause(), FileRecord.id.not_in(terminal_subq), FileRecord.id.in_(list(reviewed_by_id)))
        .order_by(FileRecord.original_filename)
        .limit(_MAX_BULK_TAG_WRITE)  # D-03: bound the operator-triggered loop at 200K scale
    )
    file_records = list((await session.execute(stmt)).scalars().all())
    validated: dict[uuid.UUID, tuple[dict[str, Any], dict[str, str | int | None]]] = {}
    for file_record in file_records:
        validated[file_record.id] = await _validate_tag_review_token(session, file_record, reviewed_by_id[file_record.id][0])
    # phaze-o2ln: snapshot every field the loop (or enqueue_tag_write) needs OUTSIDE the ORM
    # identity map, right after the SELECT and before any per-file rollback can expire it -- see
    # _BulkCandidate's docstring.
    candidates = [_snapshot_bulk_candidate(fr) for fr in file_records]
    return candidates, validated


# The closed set of per-candidate outcomes. A Literal (not a bare str) so mypy forces
# _run_bulk_candidates to stay exhaustive: an outcome added here without a matching tally branch
# is a type error, not a silent "skipped".
_BulkOutcome = Literal["queued", "noop", "skipped", "failed"]


async def _dispatch_bulk_candidate(
    session: AsyncSession,
    request: Request,
    candidate: _BulkCandidate,
    validated: dict[uuid.UUID, tuple[dict[str, Any], dict[str, str | int | None]]],
) -> _BulkOutcome:
    """Resolve ONE bulk-write candidate; returns ``"queued" | "noop" | "skipped" | "failed"``.

    phaze-oc06m review finding: the return is a ``Literal``, not a bare ``str``. The
    pre-decomposition code incremented the tallies inline, where a typo'd outcome was impossible;
    once the branch result travels as a string, an unrecognised value would fall through every
    ``elif`` in :func:`_run_bulk_candidates` and be silently counted as ``"skipped"`` -- the file
    dispatched, the operator's toast under-reporting it, and no error anywhere. The ``Literal``
    makes that a mypy failure instead.

    phaze-oc06m: extracted from :func:`bulk_write_no_discrepancies`'s per-candidate loop body to
    bring the function under the CCN/nesting bar -- a pure move, not a behavior change. Every
    branch below is byte-identical to the loop body it replaces:

    * phaze-u28m/phaze-lwqk TOCTOU re-checks (terminal / already-queued) -> ``"skipped"``, same as
      the original ``continue``.
    * WR-01 zero-change -> persist the NO_OP marker, commit, -> ``"noop"`` (caller appends the id
      to ``resolved_ids``, same as the original inline ``resolved_ids.append``).
    * A qualifying change -> dispatch via :func:`enqueue_tag_write`, commit, -> ``"queued"`` or
      ``"failed"`` by the returned log status -- same as the original ``queued += 1`` / ``failed
      += 1``.
    * phaze-k7g6: any exception -> roll back this candidate's uncommitted work only, log the same
      ``bulk_tag_write_file_skipped`` warning, -> ``"failed"`` -- same as the original ``except
      Exception`` branch.
    """
    file_id = candidate.id
    try:
        # phaze-u28m: re-check terminal status under the lock. The advisory lock blocks a
        # concurrent BULK submit, but a per-file write_file_tags could have landed a terminal log
        # for this candidate since the SELECT -- skip it rather than dispatch it twice.
        if await _has_terminal_tagwrite(session, file_id):
            return "skipped"
        # phaze-lwqk: a per-file write_file_tags/undo could ALSO have queued a job for this
        # candidate since the SELECT (queued is non-terminal, so the candidate SELECT above does
        # not exclude it) -- skip it rather than raise TagWriteAlreadyQueuedError out of
        # enqueue_tag_write below and burn a spurious "failed" tally for a file that is simply
        # already in flight.
        if await _has_queued_tagwrite(session, file_id):
            return "skipped"

        reviewed, proposed = validated[file_id]
        comparison = _build_comparison(candidate.metadata, proposed)
        if _count_changes(comparison) < 1:
            # WR-01: a zero-change applied file has nothing to write. Persist a terminal NO_OP
            # marker so ``_terminal_tagwrite_subq`` EVICTS it -- otherwise it re-occupies this same
            # window on every submit and permanently starves the qualifying files behind it.
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
            return "noop"
        if not _qualifies_for_bulk_write(comparison):
            # A >=1-change file that would blank an existing tag: never bulk-written (stays
            # per-file Approve/Edit/Skip). ``compute_proposed_tags`` never blanks, so defensive.
            return "skipped"
        tags: dict[str, str | int | list[str] | None] = {k: v for k, v in proposed.items() if v is not None}
        log_entry = await enqueue_tag_write(
            session,
            request.app.state.task_router,
            candidate,
            tags,
            source="proposal",
            reviewed_before_tags=reviewed["before"],
            review_source_versions=reviewed["sources"],
        )
        # phaze-k7g6: commit the audit row atomically with the dispatch it describes, so a
        # mid-loop cancellation/crash can never leave a job enqueued with no TagWriteLog row for
        # its agent callback to PATCH (which would strand the write silently).
        await session.commit()

        # phaze-5j82 / phaze-6bkk: count only what the server actually observed -- the hand-off.
        # The real per-file outcome arrives asynchronously via the agent callback.
        return "queued" if log_entry.status == TagWriteStatus.QUEUED else "failed"
    except Exception:
        # phaze-k7g6: roll back only this file's uncommitted work (prior per-file commits stand)
        # and keep going. A raised ValueError/TagWriteAlreadyQueuedError/DB error is a failed
        # file, not a batch abort.
        await session.rollback()
        logger.warning("bulk_tag_write_file_skipped", file_id=str(file_id), exc_info=True)
        return "failed"


async def _run_bulk_candidates(
    session: AsyncSession,
    request: Request,
    candidates: list[_BulkCandidate],
    validated: dict[uuid.UUID, tuple[dict[str, Any], dict[str, str | int | None]]],
) -> tuple[int, int, int, list[uuid.UUID]]:
    """Dispatch every candidate and tally the outcomes (phaze-oc06m: extracted, no change).

    Returns ``(queued, noop, failed, resolved_ids)`` -- ``resolved_ids`` is the phaze-gwe1 list of
    files that reached a TERMINAL state this pass (today: only the zero-change NO_OP markers,
    since a dispatched write stays non-terminal until the agent reports back).
    """
    queued = 0
    noop = 0
    failed = 0
    resolved_ids: list[uuid.UUID] = []
    for candidate in candidates:
        outcome = await _dispatch_bulk_candidate(session, request, candidate, validated)
        if outcome == "queued":
            queued += 1
        elif outcome == "noop":
            noop += 1
            resolved_ids.append(candidate.id)  # phaze-gwe1: now terminal -- remove the stale pending row
        elif outcome == "failed":
            failed += 1
        # "skipped" tallies nothing, matching the original bare ``continue``.
    return queued, noop, failed, resolved_ids


async def _bulk_write_response(
    request: Request,
    session: AsyncSession,
    queued: int,
    noop: int,
    failed: int,
    resolved_ids: list[uuid.UUID],
) -> HTMLResponse:
    """Build the final bulk-write response (phaze-oc06m: extracted, no change).

    phaze-gwe1: re-queries the SAME builder the workspace itself renders from (deferred import --
    services.review imports helpers FROM this module, so importing it back at module scope would
    cycle; by call time this module is already fully loaded) so the refreshed subcount always
    matches the row count the operator actually sees after this OOB update lands.

    phaze-bto9: this re-scan is the SECOND full pass of every submit (the first rendered the page
    the operator submitted from), so the remediation path used to pay twice over. It is now
    bounded the same way the render is -- capped candidate batches, batched per-page lookups --
    and reports a "N+" floor when the scan was truncated, matching the workspace's own subcount
    exactly.
    """
    stats = await _get_tag_stats(session)
    toast_message = _bulk_write_toast(queued, noop, failed)
    from phaze.services.review import get_tagwrite_review_page  # noqa: PLC0415 -- deferred to break the tags<->review import cycle

    remaining_page = await get_tagwrite_review_page(session)
    remaining = f"{len(remaining_page.rows)}{'+' if remaining_page.partial else ''}"
    subcount = f"{remaining} awaiting approval · the file server writes these tags"
    return templates.TemplateResponse(
        request=request,
        name="tags/partials/bulk_write_response.html",
        context={
            "request": request,
            "stats": stats,
            # phaze-6bkk: the hand-off count, not an on-disk write count -- the api never observes
            # the write. The template does not render it today; it stays for parity with the
            # already-in-progress branch above.
            "written": queued,
            "toast_message": toast_message,
            "resolved_ids": resolved_ids,
            "subcount": subcount,
            "row_id_prefix": _V7_TAGWRITE_ROW_PREFIX,
        },
    )


@router.post("/bulk-write-no-discrepancies", response_class=HTMLResponse)
async def bulk_write_no_discrepancies(
    request: Request,
    review_tokens: list[TagReviewTokenWire] = Form(..., max_length=_MAX_BULK_TAG_WRITE),
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
    blow up the loop. Each qualifying file is DISPATCHED via :func:`enqueue_tag_write`.

    phaze-6bkk: the loop no longer performs any disk I/O -- under DIST-01 it never could, since the
    api container mounts no media. It queues one ``write_file_tags`` job per qualifying file onto
    that file's OWNING agent's meta lane, so a mixed-agent candidate set fans out correctly instead
    of every write being attempted against one (non-existent) local path.

    phaze-gwe1: the pending workspace's rows are NOT re-queried/re-rendered by anything else after
    this commits (no self-poll, and the chrome ``/pipeline/stats`` poll is counts-only), so a row
    this handler resolves to a TERMINAL outcome is stale on screen: still "pending" with a live
    APPROVE that would re-dispatch it. Post-phaze-6bkk the only outcome this handler can resolve
    terminally is the zero-change NO_OP marker (which is decided from DB state alone); a QUEUED
    write is by definition not yet terminal, so its row correctly stays. The response OOB-removes
    exactly the NO_OP rows (keyed by ``tagwrite-row-{id}``, the SAME id the workspace renders) and
    refreshes the subcount.

    phaze-oc06m: the body below is decomposed into :func:`_parse_bulk_review_tokens`,
    :func:`_bulk_write_lock_busy_response`, :func:`_load_bulk_candidates`,
    :func:`_dispatch_bulk_candidate` / :func:`_run_bulk_candidates` and :func:`_bulk_write_response`
    -- a pure extract-method pass (CCN 22 -> see each helper's own docstring for the exact
    correspondence), not a behavior change. The advisory-lock acquire/release/commit/close sequence
    and its phaze-yhhy / phaze-7bjjj rationale stay inline here because they bracket the ENTIRE
    request, not any one extracted step.
    """
    if len(review_tokens) > _MAX_BULK_TAG_WRITE:
        raise HTTPException(status_code=400, detail=f"At most {_MAX_BULK_TAG_WRITE} reviewed tag decisions may be submitted")
    reviewed_by_id = _parse_bulk_review_tokens(review_tokens)
    if not reviewed_by_id:
        raise HTTPException(status_code=400, detail="Select at least one reviewed tag decision")

    # phaze-yhhy: acquire/release the SESSION-scoped advisory lock on a connection pinned for the
    # whole request, independent of how many times ``session`` itself checks its pooled connection
    # in and out via the per-file commits below.
    lock_conn, owns_lock_conn = await _bulk_tagwrite_lock_connection(session)
    try:
        # phaze-u28m: serialize the whole bulk operation. A second concurrent/duplicate submit that
        # cannot take the lock does NOTHING (no re-select, no double disk write, no duplicate audit
        # rows) rather than racing the first. Fail-fast (``pg_try_advisory_lock``) suits a
        # single-user tool: a double-click gets a clear "already in progress" toast instead of
        # silently re-tagging every file.
        if not await _acquire_bulk_tagwrite_lock(lock_conn):
            return await _bulk_write_lock_busy_response(request, session)

        # phaze-7bjjj: ``_acquire_bulk_tagwrite_lock``'s SELECT is ``lock_conn``'s first statement,
        # and this engine runs with no AUTOCOMMIT, so it auto-begins a transaction that nothing else
        # ever ends (no other op touches ``lock_conn`` before its close). Left alone, ``lock_conn``
        # sits idle-in-transaction for the whole bounded candidate loop below -- tens of seconds to
        # minutes -- holding back the backend_xmin vacuum horizon and exposed to
        # ``idle_in_transaction_session_timeout``. ``pg_try_advisory_lock`` is SESSION-, not
        # transaction-scoped (comment above), so committing here does not release it. Only when we
        # OWN ``lock_conn`` (a real, dedicated production connection): the test harness's
        # ``_bulk_tagwrite_lock_connection`` fallback reuses the hermetic per-test connection
        # (``owns_lock_conn=False``), which is a manually-managed outer transaction the test's own
        # rollback-based isolation depends on -- committing it here would durably commit test data.
        if owns_lock_conn:
            await lock_conn.commit()

        # phaze-oc06m review finding: bound BEFORE the try, as the pre-decomposition code was.
        # Today the inner try has only a ``finally``, so a raise from ``_load_bulk_candidates``
        # propagates past the ``return`` below and these can't be read unbound -- but that is safe
        # by accident, not by construction. Adding any ``except`` clause here later would turn a
        # swallowed error into an UnboundLocalError at the return instead of a zero-tally response.
        queued, noop, failed = 0, 0, 0
        resolved_ids: list[uuid.UUID] = []
        try:
            candidates, validated = await _load_bulk_candidates(session, reviewed_by_id)
            queued, noop, failed, resolved_ids = await _run_bulk_candidates(session, request, candidates, validated)
        finally:
            # phaze-yhhy: the release runs on ``lock_conn`` -- never on ``session`` -- so it can
            # never be skipped by ``session`` sitting in an aborted transaction (the candidate
            # SELECT or an early per-file failure left it there) the way the old same-connection
            # release could be.
            await _release_bulk_tagwrite_lock(lock_conn)
            # Defensive: reset ``session`` if the try block above left it mid-transaction (e.g. the
            # candidate SELECT itself raised) so the stats/subcount reads below don't inherit an
            # aborted transaction.
            await session.rollback()
    finally:
        if owns_lock_conn:
            await lock_conn.close()

    return await _bulk_write_response(request, session, queued, noop, failed, resolved_ids)


@router.post("/{file_id}/undo", response_class=HTMLResponse)
async def undo_tag_write(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """REVIEW-05 (D-04): revert a tag write by re-applying ``TagWriteLog.before_tags``.

    Reuses the EXISTING :func:`enqueue_tag_write` dispatch path (``source="undo"``) to restore the
    snapshot captured before the latest write -- NO new apply/undo logic. Appends one further
    ``TagWriteLog`` so the append-only audit trail stays coherent (REVIEW-05: every apply,
    including a reversal, is one audit row).

    phaze-6bkk: the reversal is a tag write like any other, so it too runs on the owning agent (the
    api container has no media mount). ``before_tags`` is read from the audit row -- it was captured
    on the agent at write time and reported back through the callback -- so the undo payload is
    fully determined here without touching the archive.

    phaze-y4s6: this used to fork on ``_is_v7_tagwrite_target`` the same way ``write_file_tags``
    did; the legacy ``tag_row.html`` fallback it shared with that route is gone (see
    ``write_file_tags``'s docstring), so the v7 ``_diff_row.html`` response is now the ONLY shape.
    """
    file_record = await _get_file_with_metadata(session, file_id)
    if file_record is None:
        # phaze-nvll defect 3: a stale row (file gone) gets a 200 + OOB toast, not a bare 404.
        return _tagwrite_stale_toast_response(request, "File not found -- it may have been removed or already processed.")

    # phaze-04bz: undo must be idempotent. If the most recent operation on this file was already a
    # COMPLETED reversal (an htmx double-click, or a second tab firing the still-rendered UNDO), a
    # repeat undo must be a NO-OP -- never a re-apply of the written tags -- with an honest toast.
    #
    # phaze-6bkk widens the guard to QUEUED: with the write dispatched to the agent, the window
    # between "undo pressed" and "undo COMPLETED" is now a real, observable interval rather than an
    # in-request instant, so a second click during it is the COMMON case, not a rare double-click.
    # Re-dispatching would enqueue a second reversal whose before_tags snapshot is read AFTER the
    # first one lands -- i.e. it would restore the reverted state, undoing the undo.
    #
    # phaze-lwqk: this idempotency check MUST run before ``_get_write_log_to_undo`` below. That
    # selector's chain-walk treats a COMPLETED undo as the boundary of the CURRENT write chain and
    # deliberately returns nothing for a file with no write AFTER that boundary (there is genuinely
    # nothing left to revert) -- which would otherwise reach the generic "No prior tag write to
    # undo" branch instead of this more specific "already reverted" one.
    newest = await _get_latest_write_log(session, file_id)
    if newest is not None and newest.source == "undo" and newest.status in (TagWriteStatus.COMPLETED, TagWriteStatus.QUEUED):
        in_flight = newest.status == TagWriteStatus.QUEUED
        already_message = (
            f"A revert for {file_record.original_filename} is already queued on the file server."
            if in_flight
            else f"Tags for {file_record.original_filename} were already reverted."
        )
        row_context = await _tagwrite_row_context(session, file_record, row_state="queued" if in_flight else "pending")
        return _tagwrite_diff_row_response(request, row_context, already_message)

    # phaze-soph/phaze-lwqk: target the HEAD of the current write chain -- the row whose
    # before_tags is the true pre-write state, not a later retry's shadow (see the selector's own
    # docstring for the full chain-walk rationale).
    latest = await _get_write_log_to_undo(session, file_id)
    if latest is None:
        # phaze-nvll defect 3: nothing to undo (a race/stale row) -- redraw the row as pending
        # alongside the toast rather than silently doing nothing.
        row_context = await _tagwrite_row_context(session, file_record, row_state="pending")
        return _tagwrite_diff_row_response(request, row_context, "No prior tag write to undo.")

    try:
        log_entry = await enqueue_tag_write(session, request.app.state.task_router, file_record, latest.before_tags, source="undo")
        await session.commit()
    except TagWriteAlreadyQueuedError:
        # phaze-lwqk: something else (a fresh per-file write, or the bulk loop) queued a job for
        # this file between our idempotency check above and here -- redraw as "queued" rather than
        # dispatching a second concurrent job.
        row_context = await _tagwrite_row_context(session, file_record, row_state="queued")
        already_message = f"A tag write is already queued for {file_record.original_filename}. Wait for it to finish, then retry the undo if needed."
        return _tagwrite_diff_row_response(request, row_context, already_message)
    except ValueError:
        # phaze-qe5y1: enqueue_tag_write's own is_applied guard (tag_writer.py:131-133) can fire
        # here -- the scan-deletion race (services/scan_deletion.py deletes RenameProposal,
        # TagWriteLog and FileRecord rows for a batch in one transaction) can revert the file's
        # executed proposal between our idempotency check above and this dispatch. Mirror
        # write_file_tags' is_applied handling (tags.py:524-528): redraw the row unchanged
        # (pending) alongside an honest toast, rather than letting the ValueError escape uncaught
        # as a 500 that htmx silently drops on this outerHTML-targeted row.
        row_context = await _tagwrite_row_context(session, file_record, row_state="pending")
        return _tagwrite_diff_row_response(request, row_context, "Only executed files can have tags written -- the revert could not be queued.")
    except IntegrityError:
        # phaze-qe5y1: TagWriteLog.file_id has no ondelete, and scan_deletion deletes
        # rename_proposals BEFORE tag_write_log/files, so an adjacent race window can instead trip
        # the enqueue's own audit-row INSERT (tag_writer.py's session.flush()) as an FK violation
        # rather than the is_applied guard above. That leaves the session's transaction aborted, so
        # roll back before touching it again, and treat the file as gone -- like the file_record is
        # None branch above -- rather than trying to redraw a row for a file no longer in the DB.
        await session.rollback()
        return _tagwrite_stale_toast_response(request, "File not found -- it may have been removed or already processed.")

    # phaze-26t7 / phaze-6bkk: the toast must never claim an on-disk outcome the api has not
    # observed -- and post-DIST-01 it observes none, because the reversal runs on the agent. Say
    # exactly what happened: the revert was queued, or it could not be handed off (and in that case
    # point at the agent, NOT at file permissions -- the advice that used to send operators hunting
    # a directory that simply is not mounted in this container).
    filename = file_record.original_filename
    if log_entry.status == TagWriteStatus.QUEUED:
        toast_message = f"Revert queued for {filename} on agent {file_record.agent_id}. The file server restores the previous tags; the row updates when it reports back."
    else:
        toast_message = (
            f"Undo could not be dispatched for {filename}: {log_entry.error_message or 'Unknown error'}. "
            f"Check that agent {file_record.agent_id} is reachable and its worker is running, then retry."
        )

    # phaze-nvll / phaze-6bkk: a queued revert leaves the row in "queued" (no control -- the write
    # is in flight and there is nothing coherent to press); an undispatchable one keeps "approved"
    # so UNDO stays available to retry, rather than claiming a revert that did not happen.
    row_state = "queued" if log_entry.status == TagWriteStatus.QUEUED else "approved"
    row_context = await _tagwrite_row_context(session, file_record, row_state=row_state)
    return _tagwrite_diff_row_response(request, row_context, toast_message)
