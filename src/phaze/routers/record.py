"""The canonical per-file record context, drawer fragment, and full-page view.

``GET /record/{file_id}`` and ``GET /files/{file_id}`` compose the file's existing read-only
per-file reads -- the windowed multi-lane timeline (mirrors
:func:`phaze.routers.proposals.proposal_timeline`), metadata and identity, this file's review
state, tracklist, and history -- through one context builder and one content partial. The drawer
remains a bare HTMX fragment swapped into the persistent ``record_host.html`` panel (D-01). It is
a SNAPSHOT: it renders once, carries no self-poll / ``setInterval`` / ``hx-swap-oob`` on the
approval subtree (D-02), and never re-renders the operator's in-progress edit.

Security: the ``file_id`` path param is a typed ``uuid.UUID`` (FastAPI-validated -- closes the
template-path/BAC surface, T-61-03) and EVERY read is scoped strictly by that ``file_id`` (mirrors
proposals.py:283 T-31-06-02). A missing / de-duplicated file resolves to a friendly 404 HTML fragment
(``record_not_found.html`` -- T-61-05), never a 500 / JSON detail / stack trace.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast as type_cast
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_job import CloudJob
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog
from phaze.services.agent_liveness import non_local_backend_kinds
from phaze.services.analysis_timeline import build_analysis_timeline_context
from phaze.services.pipeline import derive_file_lane, get_file_orphan_details, get_file_stage_buckets
from phaze.services.tracklist_priority import get_file_tracklist_review
from phaze.web.static import static_asset_url


if TYPE_CHECKING:
    from phaze.config import ControlSettings


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["static_url"] = static_asset_url
router = APIRouter(tags=["record"])


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


async def build_file_record_context(
    file_id: uuid.UUID,
    session: AsyncSession,
) -> dict[str, Any] | None:
    """Build the one strictly file-scoped context shared by both record presentations.

    Resolves the ``FileRecord`` by id; a missing / de-duplicated file renders the friendly 404
    response in the caller. Otherwise every read below is scoped strictly by ``file_id``
    (T-31-06-02). Keeping this assembly request- and presentation-agnostic prevents the drawer and
    canonical page from diverging on facts, eligibility, tracklists, proposals, or history.
    """
    file = await session.get(FileRecord, file_id)
    if file is None:
        return None

    # Windowed timeline -- mirror proposals.proposal_timeline (T-31-06-02 file_id scoping).
    windows_stmt = select(AnalysisWindow).where(AnalysisWindow.file_id == file_id).order_by(AnalysisWindow.tier, AnalysisWindow.window_index)
    windows = list((await session.execute(windows_stmt)).scalars().all())
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
            "proposed_path": p.proposed_path or "",
            # phaze-exivg: the optimistic-concurrency token the record page's APPROVE button
            # round-trips back to /proposals/{id}/approve.
            "updated_at": p.updated_at,
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

    # phaze-cavai: the per-stage "why" facts the pills alone cannot answer. A failed pill gets the
    # STORED failure reason (FileMetadata / AnalysisResult error_message — written on failure,
    # previously never surfaced anywhere in the UI); an orphaned enrich stage gets the ledger facts
    # that explain the strand (get_file_orphan_details — the same predicate recovery re-drives).
    # `analysis` above is this file's AnalysisResult; started-vs-never-started evidence for an
    # orphaned analyze is derived from data already loaded (partial analysis row / windows).
    metadata_row = (await session.execute(select(FileMetadata).where(FileMetadata.file_id == file_id))).scalar_one_or_none()
    stage_failure_reasons = {
        "metadata": metadata_row.error_message if metadata_row is not None else None,
        "analyze": analysis.error_message if analysis is not None else None,
    }
    orphan_details = await get_file_orphan_details(session, file_id)
    analyze_started = bool(windows) or analysis is not None

    # phaze-fq9h.8: the per-file 1001Tracklists review -- scraped/propagated/attempted-but-absent/
    # never-looked-up, plus the operator priority flag. The file was already confirmed to exist
    # above, so this can never legitimately come back None here.
    tracklist_review = await get_file_tracklist_review(session, file_id)

    # phaze-lljfx: the facts grid's Lane tile used to hardcode "local" unconditionally. Derive it
    # the SAME way the Analyze matrix does (COMPUTE-03, `derive_file_lane`) off this file's
    # (possibly absent) `CloudJob`, so the record view can never contradict the badge the operator
    # just saw for the same file. `CloudJob.file_id` is unique -- at most one row.
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    kinds = non_local_backend_kinds(type_cast("ControlSettings", get_settings()))
    lane, lane_kind = derive_file_lane(cloud_job.id if cloud_job else None, cloud_job.backend_id if cloud_job else None, kinds)

    return {
        "file": file,
        "stage_buckets": stage_buckets,
        "analysis": analysis,
        "file_id": file_id,
        **build_analysis_timeline_context(windows),
        "pending_rows": pending_rows,
        "identity": identity,
        "history": history,
        "tracklist_review": tracklist_review,
        "lane": lane,
        "lane_kind": lane_kind,
        "stage_failure_reasons": stage_failure_reasons,
        "orphan_details": orphan_details,
        "analyze_started": analyze_started,
    }


@router.get("/record/{file_id}", response_class=HTMLResponse)
async def file_record(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the existing bare, no-self-poll drawer fragment for ``file_id``."""
    context = await build_file_record_context(file_id, session)
    if context is None:
        return templates.TemplateResponse(
            request=request,
            name="record/record_not_found.html",
            context={"request": request},
            status_code=404,
        )
    return templates.TemplateResponse(
        request=request,
        name="record/record_body.html",
        context={**context, "request": request, "record_presentation": "drawer"},
    )


@router.get("/files/{file_id}", response_class=HTMLResponse)
async def file_record_page(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return an addressable full document backed by the canonical record context."""
    context = await build_file_record_context(file_id, session)
    if context is None:
        return templates.TemplateResponse(
            request=request,
            name="record/record_page.html",
            context={"request": request, "file": None, "record_presentation": "page"},
            status_code=404,
        )
    return templates.TemplateResponse(
        request=request,
        name="record/record_page.html",
        context={**context, "request": request, "record_presentation": "page"},
    )
