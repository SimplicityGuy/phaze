"""Compatibility facade for the four degrade-safe operator review read models.

Routers and documented callers keep importing from this module. Concrete SQLAlchemy readers live
in responsibility-named modules; this facade injects its collaborator globals on every call so
established test patch points such as ``review.get_proposals_page`` and
``review.generate_cue_content`` retain their resolution behavior. Every concrete reader owns its
``session.begin_nested()`` scope and never commits, rolls back, enqueues work, or writes files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from phaze.services.collision import get_review_collision_ids
from phaze.services.cue_generator import generate_cue_content
from phaze.services.cue_review import build_cue_tracks_for_versions, eligible_tracklist_stmt, gated_tracklist_stmt, get_eligible_tracklist_query
from phaze.services.dedup import GROUP_PAGE_SIZE, find_duplicate_groups_with_metadata, score_group
from phaze.services.proposal_queries import (
    count_pending_above_confidence,
    get_proposal_stats,
    get_proposals_page,
    proposal_review_digest,
)
from phaze.services.review_changes import (
    ChangesReviewPage,
    ChangesReviewStats,
    PendingProposalRows,
    ProposalWorkspacePage,
    SqlChangesReviewReader,
    _build_changes_review_row,
    _changes_review_stats,
    _changes_review_warnings,
    _pending_proposal_row,
    _proposal_row_base,
)
from phaze.services.review_cue import SqlCueReviewReader, _cue_card
from phaze.services.review_dedupe import SqlDedupeReviewReader, _format_quality, _format_size, build_dupe_group_card, dedupe_subcount_text
from phaze.services.review_tagwrite import SqlTagwriteReviewReader, TagwriteReviewPage
from phaze.services.tag_comparison import (
    _build_comparison,
    _count_changes,
    _encode_tag_review_token,
    _get_accepted_discogs_links_for_files,
    _get_tracklists_for_files,
    _summarize_tags,
    _tag_review_payload,
    _terminal_tagwrite_subq,
)
from phaze.services.tag_proposal import compute_proposed_tags


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.routers.column_sort import SortState


# These compatibility constants deliberately remain mutable facade globals. Existing tests and
# bounded-scan diagnostics patch them here; the current value is injected into each concrete reader.
_MAX_REVIEW_ROWS = 2000
_REVIEW_SCAN_BATCH = 500
_MAX_REVIEW_SCAN_BATCHES = 40

__all__ = [
    "_MAX_REVIEW_ROWS",
    "_MAX_REVIEW_SCAN_BATCHES",
    "_REVIEW_SCAN_BATCH",
    "ChangesReviewPage",
    "ChangesReviewStats",
    "PendingProposalRows",
    "ProposalWorkspacePage",
    "TagwriteReviewPage",
    "_build_changes_review_row",
    "_changes_review_stats",
    "_changes_review_warnings",
    "_cue_card",
    "_format_quality",
    "_format_size",
    "_pending_proposal_row",
    "_proposal_row_base",
    "build_dupe_group_card",
    "count_cue_review_candidates",
    "dedupe_subcount_text",
    "generate_cue_content",
    "get_changes_review_page",
    "get_cue_review_cards",
    "get_dedupe_groups",
    "get_pending_proposal_rows",
    "get_proposal_workspace_page",
    "get_proposals_page",
    "get_tagwrite_review_page",
    "get_tagwrite_review_rows",
]


def _changes_reader() -> SqlChangesReviewReader:
    return SqlChangesReviewReader(
        get_review_collision_ids=get_review_collision_ids,
        proposal_review_digest=proposal_review_digest,
        get_proposals_page=get_proposals_page,
        count_pending_above_confidence=count_pending_above_confidence,
        get_proposal_stats=get_proposal_stats,
    )


def _tagwrite_reader() -> SqlTagwriteReviewReader:
    return SqlTagwriteReviewReader(
        max_rows=_MAX_REVIEW_ROWS,
        scan_batch=_REVIEW_SCAN_BATCH,
        max_scan_batches=_MAX_REVIEW_SCAN_BATCHES,
        terminal_tagwrite_subq=_terminal_tagwrite_subq,
        get_tracklists_for_files=_get_tracklists_for_files,
        get_accepted_discogs_links_for_files=_get_accepted_discogs_links_for_files,
        compute_proposed_tags=compute_proposed_tags,
        build_comparison=_build_comparison,
        count_changes=_count_changes,
        summarize_tags=_summarize_tags,
        tag_review_payload=_tag_review_payload,
        encode_tag_review_token=_encode_tag_review_token,
    )


def _dedupe_reader() -> SqlDedupeReviewReader:
    return SqlDedupeReviewReader(
        find_duplicate_groups_with_metadata=find_duplicate_groups_with_metadata,
        score_group=score_group,
    )


def _cue_reader() -> SqlCueReviewReader:
    return SqlCueReviewReader(
        max_rows=_MAX_REVIEW_ROWS,
        eligible_tracklist_stmt=eligible_tracklist_stmt,
        gated_tracklist_stmt=gated_tracklist_stmt,
        get_eligible_tracklist_query=get_eligible_tracklist_query,
        build_cue_tracks_for_versions=build_cue_tracks_for_versions,
        generate_cue_content=generate_cue_content,
    )


async def get_changes_review_page(
    session: AsyncSession,
    *,
    status: str,
    page: int,
    page_size: int,
) -> ChangesReviewPage:
    """Return atomic filename/destination decisions under the canonical review vocabulary."""
    return await _changes_reader().get_changes_review_page(session, status=status, page=page, page_size=page_size)


async def get_pending_proposal_rows(session: AsyncSession, *, confidence_threshold: float = 0.9) -> PendingProposalRows:
    """Return the bounded pending rows and the two uncapped operator-facing totals."""
    return await _changes_reader().get_pending_proposal_rows(session, confidence_threshold=confidence_threshold)


async def get_proposal_workspace_page(
    session: AsyncSession,
    *,
    status: str,
    search: str,
    page: int,
    page_size: int,
    sort: SortState | None = None,
) -> ProposalWorkspacePage:
    """Return one filtered, searched, sorted Propose page and its tab counts."""
    return await _changes_reader().get_proposal_workspace_page(
        session,
        status=status,
        search=search,
        page=page,
        page_size=page_size,
        sort=sort,
    )


async def get_tagwrite_review_page(session: AsyncSession) -> TagwriteReviewPage:
    """Return the bounded Tag-write review page and its partial-result flag."""
    return await _tagwrite_reader().get_tagwrite_review_page(session)


async def get_tagwrite_review_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Rows-only view retained for callers that predate the page honesty flag."""
    return (await get_tagwrite_review_page(session)).rows


async def get_dedupe_groups(
    session: AsyncSession,
    *,
    limit: int = GROUP_PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return one ordered, scored duplicate-group page."""
    return await _dedupe_reader().get_dedupe_groups(session, limit=limit, offset=offset)


async def count_cue_review_candidates(session: AsyncSession) -> int:
    """Return the uncapped CUE candidate count without building preview text."""
    return await _cue_reader().count_cue_review_candidates(session)


async def get_cue_review_cards(session: AsyncSession) -> list[dict[str, Any]]:
    """Return eligible preview cards followed by gated CUE cards, without writing files."""
    return await _cue_reader().get_cue_review_cards(session)
