"""Phase 61 (61-02, RECORD-01 / D-01/D-02): the per-file full-record read-only fragment route.

``GET /record/{file_id}`` composes the file's EXISTING read-only per-file reads -- the windowed
multi-lane timeline (mirrors :func:`phaze.routers.proposals.proposal_timeline`), the metadata diff +
identity, this file's pending approvals (inline-approvable through the Phase 60 approve/edit/undo
routes), and history -- into ONE bare HTMX fragment swapped into the persistent ``record_host.html``
panel (D-01). The body is a SNAPSHOT: it renders once, carries no self-poll / ``setInterval`` /
``hx-swap-oob`` on the approval subtree (D-02), and never re-renders the operator's in-progress edit.

Security: the ``file_id`` path param is a typed ``uuid.UUID`` (FastAPI-validated -- closes the
template-path/BAC surface, T-61-03) and EVERY read is scoped strictly by that ``file_id`` (mirrors
proposals.py:283 T-31-06-02). A missing / de-duplicated file resolves to a friendly 404 HTML fragment
(``record_not_found.html`` -- T-61-05), never a 500 / JSON detail / stack trace.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog
from phaze.routers.proposals import TIMELINE_H, TIMELINE_W, _bpm_spark, _ribbons
from phaze.services.pipeline import get_file_stage_buckets


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/record", tags=["record"])


def _history_sort_key(when: datetime | None) -> tuple[bool, datetime]:
    """Stable, tz-safe sort key for merged history rows.

    ExecutionLog.executed_at decodes tz-AWARE (timestamptz) while TagWriteLog.written_at historically
    decoded tz-NAIVE (``timestamp without time zone``). ``sorted()`` over a mix of the two raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` -> a 500 on the happy path
    (every tag-written file also carries an execution log). Migration 040 aligns the DB types, but we
    ALSO normalize naive -> UTC-aware here so the merge can never throw regardless of driver decoding.
    ``None`` timestamps sort last (a half-written row never masks real history).
    """
    if when is None:
        return (False, datetime.min.replace(tzinfo=UTC))
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (True, when)


@router.get("/{file_id}", response_class=HTMLResponse)
async def file_record(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the composed, read-only full-record fragment for ``file_id`` (RECORD-01).

    Resolves the ``FileRecord`` by id; a missing / de-duplicated file renders the friendly 404
    fragment (``record_not_found.html``) with a 404 status (T-61-05). Otherwise every read below is
    scoped strictly by ``file_id`` (T-31-06-02) and the composed ``record_body.html`` snapshot is
    returned. No logic changes anywhere -- pure read + compose.
    """
    file = await session.get(FileRecord, file_id)
    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="record/record_not_found.html",
            context={"request": request},
            status_code=404,
        )

    # Windowed timeline -- mirror proposals.proposal_timeline (T-31-06-02 file_id scoping).
    windows_stmt = select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(AnalysisWindow.tier, AnalysisWindow.window_index)
    windows = list((await session.execute(windows_stmt)).scalars().all())
    fine = [w for w in windows if w.tier == "fine"]
    coarse = [w for w in windows if w.tier == "coarse"]
    total_sec = max((w.end_sec for w in windows), default=0.0)
    analysis = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()

    # Pending approvals for THIS file -- reuse the Phase 60 approve/edit/undo routes verbatim.
    #
    # ``created_at`` carries no uniqueness constraint, so two proposals for the same file can share
    # a value; with a partial ORDER BY, tied rows would come back in ANY order (heap order, which
    # shifts with page layout, vacuum, and plan choice). Appending the unique ``RenameProposal.id``
    # makes the order TOTAL and deterministic. Same rationale as the paging contract's mandatory
    # unique tiebreaker (rule 4, see :mod:`phaze.services.pagination`).
    proposals_stmt = (
        select(RenameProposal)
        .options(selectinload(RenameProposal.file))
        .where(RenameProposal.file_id == file_id, RenameProposal.status == ProposalStatus.PENDING.value)
        .order_by(RenameProposal.created_at, RenameProposal.id)
    )
    proposals = list((await session.execute(proposals_stmt)).scalars().all())
    pending_rows = [
        {
            "id": p.id,
            "filename": p.file.original_filename,
            "original_path": p.file.current_path,
            "proposed_filename": p.proposed_filename,
            "proposed_path": p.proposed_path,
        }
        for p in proposals
    ]
    # Identity section reuses proposals/partials/row_detail.html (needs the file eager-loaded).
    identity = proposals[0] if proposals else None

    # History (read-only, file_id-scoped): ExecutionLog (via its proposal) + TagWriteLog (direct).
    #
    # ``executed_at`` carries no uniqueness constraint, so two execution log rows for the same file
    # can share a value; with a partial ORDER BY, tied rows would come back in ANY order (heap
    # order, which shifts with page layout, vacuum, and plan choice). Appending the unique
    # ``ExecutionLog.id`` (DESC, matching the descending timestamp sort) makes the order TOTAL and
    # deterministic. Same rationale as the paging contract's mandatory unique tiebreaker (rule 4,
    # see :mod:`phaze.services.pagination`).
    exec_stmt = (
        select(ExecutionLog)
        .join(RenameProposal, ExecutionLog.proposal_id == RenameProposal.id)
        .where(RenameProposal.file_id == file_id)
        .order_by(ExecutionLog.executed_at.desc(), ExecutionLog.id.desc())
    )
    exec_logs = list((await session.execute(exec_stmt)).scalars().all())
    tag_stmt = select(TagWriteLog).where(TagWriteLog.file_id == file_id).order_by(TagWriteLog.written_at.desc())
    tag_logs = list((await session.execute(tag_stmt)).scalars().all())
    # Merge-sort by timestamp: concatenating two independently-DESC lists is NOT globally DESC
    # (WR-04). None timestamps sort last so a half-written row never masks real history.
    history: list[dict[str, Any]] = sorted(
        [{"when": e.executed_at, "label": e.operation, "status": e.status, "detail": e.destination_path} for e in exec_logs]
        + [{"when": t.written_at, "label": "tag write", "status": t.status, "detail": t.source} for t in tag_logs],
        key=lambda h: _history_sort_key(h["when"]),
        reverse=True,
    )

    # CONSOLE-01: the six derived per-stage buckets — the SAME stage_status_case derivation the
    # Files matrix renders, single-file-scoped, so the Stage-Eligibility pills match that row.
    stage_buckets = await get_file_stage_buckets(session, file_id)

    spark = _bpm_spark(fine, total_sec, TIMELINE_W, TIMELINE_H)
    context: dict[str, Any] = {
        "request": request,
        "file": file,
        "stage_buckets": stage_buckets,
        "analysis": analysis,
        "file_id": file_id,
        "has_windows": bool(windows),
        "total_sec": total_sec,
        "timeline_w": TIMELINE_W,
        "timeline_h": TIMELINE_H,
        "bpm_points": spark.points,
        "bpm_lo": spark.lo,
        "bpm_hi": spark.hi,
        "key_ribbons": _ribbons(fine, "musical_key", total_sec),
        "mood_ribbons": _ribbons(coarse, "mood", total_sec),
        "style_ribbons": _ribbons(coarse, "style", total_sec),
        "pending_rows": pending_rows,
        "identity": identity,
        "history": history,
    }
    return templates.TemplateResponse(request=request, name="record/record_body.html", context=context)
