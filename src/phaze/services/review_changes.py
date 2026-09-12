"""Read models for the Changes Review, Rename/Move, and Propose operator workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload
import structlog

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.proposal_queries import Pagination, ProposalStats


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class PendingProposalRows(NamedTuple):
    """Pending proposal rows plus the corpus-wide totals used by their headers."""

    rows: list[dict[str, Any]]
    total_pending: int
    high_confidence_pending: int


class ChangesReviewStats(NamedTuple):
    """Canonical operator vocabulary over persisted proposal states."""

    all: int
    needs_review: int
    approved: int
    blocked: int
    rejected: int


class ChangesReviewPage(NamedTuple):
    """One bounded Changes Review page and its corpus-wide status counts."""

    rows: list[dict[str, Any]]
    pagination: Pagination
    stats: ChangesReviewStats


class ProposalWorkspacePage(NamedTuple):
    """One filtered Propose page and the tab counts from the same read boundary."""

    rows: list[dict[str, Any]]
    pagination: Pagination
    stats: ProposalStats


class ChangesReviewReader(Protocol):
    """Consumer-facing port for proposal-backed review workspace reads."""

    async def get_changes_review_page(
        self,
        session: AsyncSession,
        *,
        status: str,
        page: int,
        page_size: int,
    ) -> ChangesReviewPage: ...

    async def get_pending_proposal_rows(self, session: AsyncSession, *, confidence_threshold: float = 0.9) -> PendingProposalRows: ...

    async def get_proposal_workspace_page(
        self,
        session: AsyncSession,
        *,
        status: str,
        search: str,
        page: int,
        page_size: int,
        sort: object | None = None,
    ) -> ProposalWorkspacePage: ...


_CHANGES_STATUS_MAP: dict[str, tuple[ProposalStatus, ...]] = {
    "needs_review": (ProposalStatus.PENDING,),
    "approved": (ProposalStatus.APPROVED, ProposalStatus.EXECUTED),
    "blocked": (ProposalStatus.FAILED,),
    "rejected": (ProposalStatus.REJECTED,),
}


async def _changes_review_stats(session: AsyncSession) -> ChangesReviewStats:
    count_stmt = select(
        func.count().label("all"),
        func.count(case((RenameProposal.status == ProposalStatus.PENDING.value, 1))).label("needs_review"),
        func.count(case((RenameProposal.status.in_((ProposalStatus.APPROVED.value, ProposalStatus.EXECUTED.value)), 1))).label("approved"),
        func.count(case((RenameProposal.status == ProposalStatus.FAILED.value, 1))).label("blocked"),
        func.count(case((RenameProposal.status == ProposalStatus.REJECTED.value, 1))).label("rejected"),
    ).select_from(RenameProposal)
    aggregate = (await session.execute(count_stmt)).one()
    return ChangesReviewStats(
        all=aggregate.all,
        needs_review=aggregate.needs_review,
        approved=aggregate.approved,
        blocked=aggregate.blocked,
        rejected=aggregate.rejected,
    )


def _changes_review_warnings(proposal: RenameProposal) -> list[str]:
    warnings: list[str] = []
    if proposal.confidence is None:
        warnings.append("Confidence unavailable; individual review required.")
    elif proposal.confidence < 0.9:
        warnings.append("Below the 90% bulk-approval threshold; individual review required.")
    if proposal.proposed_path is None:
        warnings.append("No destination change; the file will be renamed in its current directory.")
    if proposal.reason:
        warnings.append(proposal.reason)
    return warnings


def _build_changes_review_row(
    proposal: RenameProposal,
    collision_ids: set[str],
    proposal_review_digest: Callable[[RenameProposal], str],
) -> dict[str, Any]:
    raw_status = ProposalStatus(proposal.status)
    review_status = next((label for label, values in _CHANGES_STATUS_MAP.items() if raw_status in values), "blocked")
    conflicts = ["Destination collides with another pending or approved operation; approval is blocked."] if str(proposal.id) in collision_ids else []
    return {
        "id": proposal.id,
        "file_id": proposal.file_id,
        "filename": proposal.file.original_filename,
        "current_path": proposal.file.current_path,
        "proposed_filename": proposal.proposed_filename,
        "proposed_path": proposal.proposed_path,
        "confidence": proposal.confidence,
        "status": review_status,
        "raw_status": proposal.status,
        "warnings": _changes_review_warnings(proposal),
        "conflicts": conflicts,
        "bulk_eligible": (raw_status == ProposalStatus.PENDING and proposal.confidence is not None and proposal.confidence >= 0.9 and not conflicts),
        "updated_at": proposal.updated_at,
        "review_token": f"{proposal.id}|{proposal.updated_at.isoformat()}|{proposal_review_digest(proposal)}",
        "set_profile": proposal.file.set_profile,
    }


def _proposal_row_base(proposal: RenameProposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "filename": proposal.file.original_filename,
        "current_path": proposal.file.current_path,
        "proposed_filename": proposal.proposed_filename,
        "proposed_path": proposal.proposed_path,
        "confidence": proposal.confidence,
        "status": proposal.status,
    }


def _pending_proposal_row(proposal: RenameProposal) -> dict[str, Any]:
    row = _proposal_row_base(proposal)
    row["updated_at"] = proposal.updated_at
    return row


class SqlChangesReviewReader:
    """SQLAlchemy implementation of :class:`ChangesReviewReader`.

    Collaborators are injected by the compatibility facade so patches applied at
    ``phaze.services.review`` continue to intercept the same calls.
    """

    def __init__(
        self,
        *,
        get_review_collision_ids: Callable[[AsyncSession], Awaitable[set[str]]],
        proposal_review_digest: Callable[[RenameProposal], str],
        get_proposals_page: Callable[..., Awaitable[tuple[list[RenameProposal], Pagination]]],
        count_pending_above_confidence: Callable[..., Awaitable[int]],
        get_proposal_stats: Callable[[AsyncSession], Awaitable[ProposalStats]],
    ) -> None:
        self._get_review_collision_ids = get_review_collision_ids
        self._proposal_review_digest = proposal_review_digest
        self._get_proposals_page = get_proposals_page
        self._count_pending_above_confidence = count_pending_above_confidence
        self._get_proposal_stats = get_proposal_stats

    async def get_changes_review_page(
        self,
        session: AsyncSession,
        *,
        status: str,
        page: int,
        page_size: int,
    ) -> ChangesReviewPage:
        active_status = status if status in {"all", *_CHANGES_STATUS_MAP} else "needs_review"
        try:
            async with session.begin_nested():
                stats = await _changes_review_stats(session)
                query = select(RenameProposal).options(selectinload(RenameProposal.file).selectinload(FileRecord.set_profile))
                if active_status != "all":
                    query = query.where(RenameProposal.status.in_(tuple(one.value for one in _CHANGES_STATUS_MAP[active_status])))
                filtered_total = getattr(stats, active_status) if active_status != "all" else stats.all
                total_pages = max(1, (filtered_total + page_size - 1) // page_size)
                current_page = min(max(page, 1), total_pages)
                query = (
                    query.order_by(RenameProposal.confidence.asc().nulls_first(), RenameProposal.id)
                    .offset((current_page - 1) * page_size)
                    .limit(page_size)
                )
                proposals = (await session.execute(query)).scalars().all()
                collision_ids = await self._get_review_collision_ids(session)
                rows = [_build_changes_review_row(proposal, collision_ids, self._proposal_review_digest) for proposal in proposals]
                return ChangesReviewPage(
                    rows=rows,
                    pagination=Pagination(page=current_page, page_size=page_size, total=filtered_total),
                    stats=stats,
                )
        except Exception:
            logger.warning("changes_review_page_degraded", exc_info=True)
            return ChangesReviewPage(
                rows=[],
                pagination=Pagination(page=1, page_size=page_size, total=0),
                stats=ChangesReviewStats(all=0, needs_review=0, approved=0, blocked=0, rejected=0),
            )

    async def get_pending_proposal_rows(self, session: AsyncSession, *, confidence_threshold: float = 0.9) -> PendingProposalRows:
        try:
            async with session.begin_nested():
                proposals, pagination = await self._get_proposals_page(session, status="pending", page_size=200)
                high_confidence_pending = await self._count_pending_above_confidence(session, threshold=confidence_threshold)
                rows = [_pending_proposal_row(proposal) for proposal in proposals]
                return PendingProposalRows(rows=rows, total_pending=pagination.total, high_confidence_pending=high_confidence_pending)
        except Exception:
            logger.warning("pending_proposal_rows_degraded", exc_info=True)
            return PendingProposalRows(rows=[], total_pending=0, high_confidence_pending=0)

    async def get_proposal_workspace_page(
        self,
        session: AsyncSession,
        *,
        status: str,
        search: str,
        page: int,
        page_size: int,
        sort: object | None = None,
    ) -> ProposalWorkspacePage:
        try:
            async with session.begin_nested():
                proposals, pagination = await self._get_proposals_page(
                    session,
                    status=status,
                    search=search or None,
                    page=page,
                    page_size=page_size,
                    sort=sort,
                )
                stats = await self._get_proposal_stats(session)
                rows = [_proposal_row_base(proposal) for proposal in proposals]
                return ProposalWorkspacePage(rows=rows, pagination=pagination, stats=stats)
        except Exception:
            logger.warning("proposal_workspace_page_degraded", exc_info=True)
            return ProposalWorkspacePage(
                rows=[],
                pagination=Pagination(page=1, page_size=page_size, total=0),
                stats=ProposalStats(total=0, pending=0, approved=0, executed=0, rejected=0, failed=0, avg_confidence=None),
            )
