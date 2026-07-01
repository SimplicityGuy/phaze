"""Phase 60 (60-02, REVIEW-01/REVIEW-02): degrade-safe read helper for the Review diff workspaces.

The Rename/Path and Move-files workspaces both render pending ``RenameProposal`` rows through the
ONE shared ``pipeline/partials/_diff_row.html`` partial (D-06). This helper is their single read
seam: it wraps the existing :func:`phaze.services.proposal_queries.get_proposals_page` in a
SAVEPOINT and maps each ORM row (with its eager-loaded file) to a plain dict, so the templates never
touch an ORM object and the hot render/poll path can NEVER 500 (mirrors
:func:`phaze.services.pipeline.get_analyze_stage_files`). No enqueue, no commit, no schema change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from phaze.services.proposal_queries import get_proposals_page


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
