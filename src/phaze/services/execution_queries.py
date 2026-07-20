"""Query service for audit log page -- pagination, filtering, stats."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, func, select
import structlog

from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.services.pagination import DEFAULT_PAGE_SIZE, Page, clamp_page, clamp_page_size, paged_stmt, split_sentinel


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


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
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page[ExecutionLog]:
    """Return ONE bounded page of the audit log ordered by executed_at DESC (paging contract, phaze-mft5).

    ``executed_at`` is populated purely by ``server_default=func.now()`` (models/execution.py) --
    Postgres timestamp defaults are transaction-time constant, so every row written by a bulk audit
    write in the SAME transaction ties EXACTLY, to the microsecond. That is the worst case for OFFSET
    paging: without a unique tiebreaker Postgres may order the tied block differently between the
    query that renders page N and the one that renders page N+1, silently DUPLICATING one row across
    the boundary while another is SKIPPED entirely -- an audit entry a reviewer never sees. This is an
    append-only audit trail, so completeness of the paginated view is the whole point.

    ``ExecutionLog.id`` is a client-generated ``uuid.uuid4`` -- random, not sequential, so it does not
    order ties by recency -- but it is the only column guaranteed UNIQUE across the tied block, which
    is all the paging contract's tiebreaker (rule 4) requires: a deterministic total order so OFFSET
    paging can never skip or duplicate a row.

    SAVEPOINT degrade-safe (contract rule 6): returns an EMPTY page on any error rather than 500ing
    the audit view.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)

    stmt = select(ExecutionLog)
    if status is not None and status != "all":
        stmt = stmt.where(ExecutionLog.status == status)
    stmt = paged_stmt(
        stmt,
        page=page,
        page_size=page_size,
        order_by=(ExecutionLog.executed_at.desc(),),
        tiebreaker=(ExecutionLog.id.desc(),),
    )

    try:
        async with session.begin_nested():
            raw = (await session.execute(stmt)).scalars().all()
    except Exception:
        logger.warning("audit_log_page_degraded", status=status, page=page, page_size=page_size, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)

    rows, has_next = split_sentinel(raw, page_size)
    return Page(rows=rows, page=page, page_size=page_size, has_next=has_next)
