"""Query service for audit log page -- pagination, filtering, stats."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, func, select

from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.services.proposal_queries import Pagination


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_execution_stats(session: AsyncSession) -> dict[str, int]:
    """Get aggregate execution log statistics in a single query."""
    stmt = select(
        func.count().label("total"),
        func.count(case((ExecutionLog.status == ExecutionStatus.COMPLETED, 1))).label("completed"),
        func.count(case((ExecutionLog.status == ExecutionStatus.FAILED, 1))).label("failed"),
        func.count(case((ExecutionLog.status == ExecutionStatus.IN_PROGRESS, 1))).label("in_progress"),
    ).select_from(ExecutionLog)

    result = await session.execute(stmt)
    row = result.one()
    return {
        "total": row.total,
        "completed": row.completed,
        "failed": row.failed,
        "in_progress": row.in_progress,
    }


async def get_execution_logs_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ExecutionLog], Pagination]:
    """Get a paginated, filtered page of execution logs ordered by executed_at DESC."""
    base = select(ExecutionLog)
    count_base = select(func.count()).select_from(ExecutionLog)

    # Status filter
    if status is not None and status != "all":
        base = base.where(ExecutionLog.status == status)
        count_base = count_base.where(ExecutionLog.status == status)

    # Order by most recent first
    base = base.order_by(ExecutionLog.executed_at.desc())

    # Count total
    count_result = await session.execute(count_base)
    total = count_result.scalar_one()

    # Paginate
    base = base.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(base)
    logs = list(result.scalars().all())

    pagination = Pagination(page=page, page_size=page_size, total=total)
    return logs, pagination
