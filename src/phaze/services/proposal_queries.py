"""Query service for proposal list page -- pagination, filtering, stats."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import selectinload

from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    import uuid as uuid_mod

    from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord


@dataclass
class Pagination:
    """Pagination metadata for a page of results."""

    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        """Total number of pages."""
        if self.total == 0:
            return 1
        return math.ceil(self.total / self.page_size)

    @property
    def has_prev(self) -> bool:
        """Whether a previous page exists."""
        return self.page > 1

    @property
    def has_next(self) -> bool:
        """Whether a next page exists."""
        return self.page < self.total_pages

    @property
    def start(self) -> int:
        """1-based index of the first item on this page."""
        if self.total == 0:
            return 0
        return (self.page - 1) * self.page_size + 1

    @property
    def end(self) -> int:
        """1-based index of the last item on this page."""
        return min(self.page * self.page_size, self.total)


@dataclass
class ProposalStats:
    """Aggregate statistics for proposals."""

    total: int
    pending: int
    approved: int
    rejected: int
    avg_confidence: float | None


async def get_proposal_stats(session: AsyncSession) -> ProposalStats:
    """Get aggregate proposal statistics in a single query."""
    stmt = select(
        func.count().label("total"),
        func.count(case((RenameProposal.status == ProposalStatus.PENDING, 1))).label("pending"),
        func.count(case((RenameProposal.status == ProposalStatus.APPROVED, 1))).label("approved"),
        func.count(case((RenameProposal.status == ProposalStatus.REJECTED, 1))).label("rejected"),
        func.avg(RenameProposal.confidence).label("avg_confidence"),
    ).select_from(RenameProposal)

    result = await session.execute(stmt)
    row = result.one()
    return ProposalStats(
        total=row.total,
        pending=row.pending,
        approved=row.approved,
        rejected=row.rejected,
        avg_confidence=float(row.avg_confidence) if row.avg_confidence is not None else None,
    )


async def get_proposals_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = "confidence",
    sort_order: str = "asc",
) -> tuple[list[RenameProposal], Pagination]:
    """Get a paginated, filtered, sorted page of proposals with eager-loaded file data."""
    base = select(RenameProposal).options(selectinload(RenameProposal.file))
    count_base = select(func.count()).select_from(RenameProposal)

    # Status filter
    if status is not None and status != "all":
        base = base.where(RenameProposal.status == status)
        count_base = count_base.where(RenameProposal.status == status)

    # Search filter
    if search and search.strip():
        search_term = f"%{search.strip()}%"
        search_filter = or_(
            RenameProposal.proposed_filename.ilike(search_term),
            RenameProposal.file_id.in_(
                select(FileRecord.id).where(FileRecord.original_filename.ilike(search_term))
            ),
        )
        base = base.where(search_filter)
        count_base = count_base.where(search_filter)

    # Sorting
    valid_sort_columns = {"confidence", "proposed_filename", "original_filename"}
    if sort_by not in valid_sort_columns:
        sort_by = "confidence"

    sort_col: Any
    if sort_by == "original_filename":
        base = base.join(RenameProposal.file)
        sort_col = FileRecord.original_filename
    elif sort_by == "proposed_filename":
        sort_col = RenameProposal.proposed_filename
    else:
        sort_col = RenameProposal.confidence

    base = base.order_by(sort_col.desc() if sort_order == "desc" else sort_col.asc())

    # Count total
    count_result = await session.execute(count_base)
    total = count_result.scalar_one()

    # Paginate
    base = base.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(base)
    proposals = list(result.scalars().all())

    pagination = Pagination(page=page, page_size=page_size, total=total)
    return proposals, pagination


async def update_proposal_status(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
    new_status: ProposalStatus,
) -> RenameProposal | None:
    """Update a single proposal's status and return it with eagerly loaded file."""
    stmt = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result = await session.execute(stmt)
    proposal = result.scalar_one_or_none()
    if proposal is None:
        return None
    proposal.status = new_status.value
    await session.commit()
    # Re-fetch with selectinload to ensure file relationship is available
    # (session.refresh does not honor selectinload on lazy='raise' relationships)
    stmt2 = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result2 = await session.execute(stmt2)
    return result2.scalar_one_or_none()


async def bulk_update_status(
    session: AsyncSession,
    proposal_ids: list[uuid_mod.UUID],
    new_status: ProposalStatus,
) -> int:
    """Bulk-update status for multiple proposals. Returns number of rows updated."""
    stmt = update(RenameProposal).where(RenameProposal.id.in_(proposal_ids)).values(status=new_status.value)
    cursor_result: Any = await session.execute(stmt)
    await session.commit()
    return int(cursor_result.rowcount)


async def get_proposal_with_file(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
) -> RenameProposal | None:
    """Get a single proposal with its associated file record."""
    stmt = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
