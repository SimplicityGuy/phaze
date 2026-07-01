"""Phase 60 (REVIEW-01/REVIEW-02): degrade-safe read helpers for the Review diff workspaces.

The Rename/Path and Move-files workspaces (Plan 60-02) render pending ``RenameProposal`` rows, and the
Tag-write workspace (Plan 60-03) renders the computed tag comparison, all through the ONE shared
``pipeline/partials/_diff_row.html`` partial (D-06). These helpers are their single read seam: each
wraps its query in a ``session.begin_nested()`` SAVEPOINT and maps every ORM row to a plain dict, so
the templates never touch an ORM object and the hot render/poll path can NEVER 500 (mirrors
:func:`phaze.services.pipeline.get_analyze_stage_files`). No enqueue, no commit, no schema change.

* :func:`get_pending_proposal_rows` -- pending ``RenameProposal`` rows (Rename/Move, Plan 60-02).
* :func:`get_tagwrite_review_rows`  -- EXECUTED files with a pending, >=1-change tag comparison
  (Tag-write, Plan 60-03; Pitfall 3 -- only EXECUTED files without a COMPLETED ``TagWriteLog``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload
import structlog

from phaze.models.file import FileRecord, FileState
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.routers.tags import (
    _build_comparison,
    _count_changes,
    _get_accepted_discogs_link,
    _get_tracklist_for_file,
)
from phaze.services.proposal_queries import get_proposals_page
from phaze.services.tag_proposal import compute_proposed_tags


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def get_pending_proposal_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Return pending ``RenameProposal`` rows as plain dicts for the diff workspaces (degrade-safe).

    Reuses ``get_proposals_page(status="pending")`` inside a ``session.begin_nested()`` SAVEPOINT and
    maps each proposal (plus its ``selectinload``'d file) to a plain dict keyed for both diff facets:
    ``id`` · ``filename`` (``file.original_filename``) · ``original_path`` (``file.current_path``) ·
    ``proposed_filename`` · ``proposed_path`` · ``confidence``. Returns ``[]`` on any DB error so the
    render/poll path degrades instead of 500ing (no router try/except needed).
    """
    try:
        async with session.begin_nested():
            proposals, _pagination = await get_proposals_page(session, status="pending", page_size=200)
            return [
                {
                    "id": proposal.id,
                    "filename": proposal.file.original_filename,
                    "original_path": proposal.file.current_path,
                    "proposed_filename": proposal.proposed_filename,
                    "proposed_path": proposal.proposed_path,
                    "confidence": proposal.confidence,
                }
                for proposal in proposals
            ]
    except Exception:
        logger.warning("pending_proposal_rows_degraded", exc_info=True)
        return []


def _summarize_tags(comparison: list[dict[str, Any]], side: str) -> str:
    """Join a comparison's ``current`` (before) or ``proposed`` (after) side into a display string.

    Renders ``"label: value · label: value · …"`` across every CORE field, with an em dash for a
    ``None`` value (an absent tag). ``side`` is ``"current"`` or ``"proposed"``. All values are plain
    Python data -- the caller's template autoescapes them on render (T-60-XSS).
    """
    parts = [f"{c['label']}: {c[side] if c[side] is not None else '—'}" for c in comparison]
    return " · ".join(parts)


async def get_tagwrite_review_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Return the pending tag-write review rows as plain dicts for the Tag-write workspace (degrade-safe).

    Surfaces ONLY ``EXECUTED`` files that have NO ``COMPLETED`` ``TagWriteLog`` (Pitfall 3 -- a file
    still awaiting a move never appears, so an empty queue is CORRECT, not a bug) and whose
    server-computed tag comparison has ``>= 1`` change (there is something to write). For each it mirrors
    ``tags.list_tags``: ``compute_proposed_tags`` over the file's metadata + tracklist + accepted Discogs
    link, then ``_build_comparison`` / ``_count_changes``. The whole read runs inside a
    ``session.begin_nested()`` SAVEPOINT and returns ``[]`` on any error so the render/poll path degrades
    instead of 500ing (no router try/except needed). Per row: ``file_id`` · ``filename`` ·
    ``before_summary`` (current tags joined) · ``after_summary`` (proposed tags joined) · ``changed_count``
    · ``has_blanking`` (any field whose current value would be erased). No enqueue, no commit, no write.
    """
    try:
        async with session.begin_nested():
            completed_subq = select(TagWriteLog.file_id).where(TagWriteLog.status == TagWriteStatus.COMPLETED)
            stmt = (
                select(FileRecord)
                .options(selectinload(FileRecord.file_metadata))
                .where(FileRecord.state == FileState.EXECUTED, FileRecord.id.not_in(completed_subq))
                .order_by(FileRecord.original_filename)
            )
            file_records = list((await session.execute(stmt)).scalars().all())

            rows: list[dict[str, Any]] = []
            for fr in file_records:
                tracklist = await _get_tracklist_for_file(session, fr.id)
                discogs_link = await _get_accepted_discogs_link(session, fr.id)
                proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename, discogs_link=discogs_link)
                comparison = _build_comparison(fr.file_metadata, proposed)
                changed_count = _count_changes(comparison)
                if changed_count < 1:
                    continue
                rows.append(
                    {
                        "file_id": fr.id,
                        "filename": fr.original_filename,
                        "before_summary": _summarize_tags(comparison, "current"),
                        "after_summary": _summarize_tags(comparison, "proposed"),
                        "changed_count": changed_count,
                        "has_blanking": any(c["current"] is not None and c["proposed"] is None for c in comparison),
                    }
                )
            return rows
    except Exception:
        logger.warning("tagwrite_review_rows_degraded", exc_info=True)
        return []
