"""Query service for proposal list page -- pagination, filtering, stats."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from collections.abc import Iterable
    import uuid as uuid_mod

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.routers.column_sort import SortState

from phaze.models.file import FileRecord


class ProposalTransitionError(Exception):
    """A proposal status write blocked by the documented state machine (phaze-uu17).

    Carries the current and attempted statuses so routers can translate it into a
    409 without re-querying. Terminal EXECUTED/FAILED rows (the authoritative record
    that a rename was applied) must never be flipped back to pending/approved/rejected.
    """

    def __init__(self, current_status: str, attempted_status: str) -> None:
        super().__init__(f"illegal transition {current_status} -> {attempted_status}")
        self.current_status = current_status
        self.attempted_status = attempted_status


class ProposalPendingConflictError(Exception):
    """Reverting a proposal to PENDING would violate the one-pending-per-file index (phaze-uu17)."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"file already has a pending proposal (proposal {proposal_id})")
        self.proposal_id = proposal_id


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
    sort: SortState | None = None,
) -> tuple[list[RenameProposal], Pagination]:
    """Get a paginated, filtered, sorted page of proposals with eager-loaded file data.

    ``sort`` is a RESOLVED :class:`~phaze.routers.column_sort.SortState`, never a raw wire string
    (phaze-a6hm.10). This function therefore holds NO whitelist of its own: it used to map a
    ``sort_by`` string to a column through an ``if``/``elif`` ladder, which was a second, drifting
    implementation of the shared sortable-column contract and the exact defect that contract's rule
    2 exists to close. Resolution -- and with it the guarantee that an untrusted string never
    reaches a column -- now happens once, in the caller, against
    :data:`~phaze.routers.proposal_sort.PROPOSAL_SORT_COLUMNS`.

    ``None`` means "no operator preference", answered with the default confidence ordering rather
    than an unordered read: an OFFSET page over an unordered query is not a stable page.
    """
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
            RenameProposal.file_id.in_(select(FileRecord.id).where(FileRecord.original_filename.ilike(search_term))),
        )
        base = base.where(search_filter)
        count_base = count_base.where(search_filter)

    # Sorting. The join is UNCONDITIONAL now that the ORDER BY is opaque: the whitelist may hand
    # back a FileRecord column (`original_filename`) and this function no longer knows which key it
    # resolved, so it can no longer decide per-key whether the join is needed. Joining always is
    # safe rather than merely convenient -- `RenameProposal.file_id` is a NOT NULL foreign key to
    # `files.id`, so this INNER join is row-preserving by schema constraint and cannot silently
    # drop proposals the old conditional join kept.
    base = base.join(RenameProposal.file)

    # `sort.order_by()` is the ONLY place a direction becomes SQL, and it can only ever yield an
    # expression some developer enumerated at import time. The trailing `RenameProposal.id` is a
    # TIEBREAKER, not display order (paging contract rule 4): the operator-chosen key ties often --
    # `confidence` is nullable and coarse, `proposed_path` repeats across a whole album -- and
    # OFFSET paging over a non-total order lets a row appear on two pages or none.
    order_by = sort.order_by() if sort is not None else (RenameProposal.confidence.asc(),)
    base = base.order_by(*order_by, RenameProposal.id.asc())

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
    allowed_from: Iterable[ProposalStatus] | None = None,
) -> RenameProposal | None:
    """Update a single proposal's status and return it with eagerly loaded file.

    ``allowed_from`` gates the write to the documented state machine (phaze-uu17):
    when provided, a proposal whose current status is not in the set raises
    :class:`ProposalTransitionError` (routers translate it to 409) instead of
    silently overwriting a terminal EXECUTED/FAILED row. ``None`` preserves the
    legacy unconditional write for internal callers that already pre-filter.

    phaze-upnj: the guard is evaluated INSIDE the UPDATE's WHERE clause, in one
    statement (mirroring :func:`bulk_update_status`), NOT as a Python check on a
    prior unlocked SELECT. The former select-check-then-blind-write shape was a
    TOCTOU: under READ COMMITTED an operator Undo and a concurrent agent
    EXECUTED report both read APPROVED, both passed the Python guard, and the
    last committer silently reverted the terminal record. Folding ``allowed_from``
    into the UPDATE predicate makes the write itself conditional, so a row that
    left the allowed set between any read and this write matches zero rows and
    the transition is refused rather than clobbered.
    """
    stmt = update(RenameProposal).where(RenameProposal.id == proposal_id)
    if allowed_from is not None:
        stmt = stmt.where(RenameProposal.status.in_([s.value for s in allowed_from]))
    stmt = stmt.values(status=new_status.value)
    try:
        cursor_result: Any = await session.execute(stmt)
        await session.commit()
    except IntegrityError as exc:
        # Reverting to PENDING can collide with the one-pending-per-file partial unique
        # index (uq_proposals_file_id_pending) if the file already has a pending proposal.
        await session.rollback()
        raise ProposalPendingConflictError(str(proposal_id)) from exc

    if int(cursor_result.rowcount) == 0:
        # The conditional UPDATE matched nothing: either the proposal does not exist,
        # or (when allowed_from is set) its current status is outside the allowed set.
        # Re-read to distinguish 404 (None) from an illegal transition (409).
        current = await session.execute(select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id))
        proposal = current.scalar_one_or_none()
        if proposal is None:
            return None
        if allowed_from is not None:
            raise ProposalTransitionError(proposal.status, new_status.value)
        return proposal

    # Re-fetch with selectinload to ensure file relationship is available
    # (session.refresh does not honor selectinload on lazy='raise' relationships)
    stmt2 = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result2 = await session.execute(stmt2)
    return result2.scalar_one_or_none()


async def bulk_update_status(
    session: AsyncSession,
    proposal_ids: list[uuid_mod.UUID],
    new_status: ProposalStatus,
    allowed_from: Iterable[ProposalStatus] | None = None,
) -> int:
    """Bulk-update status for multiple proposals. Returns number of rows updated.

    ``allowed_from`` constrains the UPDATE to rows in one of those from-states
    (phaze-uu17): terminal EXECUTED/FAILED rows selected by an "All"-tab bulk
    action are skipped rather than rewritten, and the returned count reflects only
    the rows that actually transitioned.
    """
    stmt = update(RenameProposal).where(RenameProposal.id.in_(proposal_ids))
    if allowed_from is not None:
        stmt = stmt.where(RenameProposal.status.in_([s.value for s in allowed_from]))
    stmt = stmt.values(status=new_status.value)
    cursor_result: Any = await session.execute(stmt)
    await session.commit()
    return int(cursor_result.rowcount)


async def approve_pending_above_confidence(session: AsyncSession, threshold: float = 0.9) -> int:
    """Approve every PENDING proposal whose confidence >= threshold (server-evaluated predicate).

    REVIEW-02 / D-02: the caller passes NO id-list; the server re-queries the pending rows that
    meet the fixed confidence predicate at submit and bulk-updates them to APPROVED. Rows whose
    ``confidence`` IS NULL are excluded by the SQL comparison (Pitfall 2 -- the conservative-correct
    behavior for an irreplaceable archive; do NOT COALESCE), leaving them for per-file review.
    Reuses :func:`bulk_update_status` so the ``proposals.status`` write is identical to the existing
    bulk path. Returns the number of proposals approved.

    phaze-bg4w: the ``allowed_from=[PENDING]`` guard is passed through to ``bulk_update_status`` so
    the from-state predicate lands INSIDE the UPDATE's WHERE clause. Without it the UPDATE carried
    only ``WHERE id IN (:ids)`` -- the ids snapshotted by the SELECT above -- so a proposal a
    concurrent tab REJECTED between this function's SELECT and UPDATE was silently flipped back to
    APPROVED (the same TOCTOU the single-row and existing bulk paths already close).
    """
    stmt = select(RenameProposal.id).where(
        RenameProposal.status == ProposalStatus.PENDING,
        RenameProposal.confidence >= threshold,
    )
    ids = list((await session.execute(stmt)).scalars().all())
    if not ids:
        return 0
    return await bulk_update_status(session, ids, ProposalStatus.APPROVED, allowed_from=frozenset({ProposalStatus.PENDING}))


async def update_proposal_fields(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
    *,
    proposed_filename: str | None = None,
    proposed_path: str | None = None,
    allowed_from: Iterable[ProposalStatus] | None = None,
) -> RenameProposal | None:
    """Persist an operator edit to a proposal's ``proposed_filename`` / ``proposed_path`` (D-05).

    Mirrors :func:`update_proposal_status` -- an atomic conditional UPDATE -- but mutates the
    provided Text field(s) instead of ``.status``. The row keeps its status (edit is pre-approve --
    no status change) and the LLM is NOT re-run. Keeps the re-select-with-``selectinload(file)``
    tail so the returned row can render its diff. Returns ``None`` if the proposal does not exist.

    phaze-3tj4: ``allowed_from`` gates the edit to the given from-states, evaluated INSIDE the
    UPDATE's WHERE clause. The docstring's long-claimed "the row stays PENDING (edit is pre-approve)"
    invariant used to be prose-only: the write was unconditional, so an edit that landed after a
    concurrent approval silently rewrote the ``proposed_path`` an APPROVED row feeds straight into
    ``execution_dispatch`` -- redirecting a reviewed move to an unreviewed destination -- and edits
    to terminal EXECUTED/FAILED rows corrupted the historical record. With ``allowed_from`` the
    edit refuses (``ProposalTransitionError`` -> 409) rather than mutating a non-editable row.
    """
    values: dict[str, Any] = {}
    if proposed_filename is not None:
        values["proposed_filename"] = proposed_filename
    if proposed_path is not None:
        values["proposed_path"] = proposed_path

    stmt = update(RenameProposal).where(RenameProposal.id == proposal_id)
    if allowed_from is not None:
        stmt = stmt.where(RenameProposal.status.in_([s.value for s in allowed_from]))
    stmt = stmt.values(**values)
    cursor_result: Any = await session.execute(stmt)
    await session.commit()

    if int(cursor_result.rowcount) == 0:
        # No row matched: 404 (proposal gone) vs 409 (status outside allowed_from).
        current = await session.execute(select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id))
        proposal = current.scalar_one_or_none()
        if proposal is None:
            return None
        if allowed_from is not None:
            raise ProposalTransitionError(proposal.status, proposal.status)
        return proposal

    # Re-fetch with selectinload to ensure the file relationship is available for the row render
    # (session.refresh does not honor selectinload on lazy='raise' relationships).
    stmt2 = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result2 = await session.execute(stmt2)
    return result2.scalar_one_or_none()


async def get_proposal_with_file(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
) -> RenameProposal | None:
    """Get a single proposal with its associated file record."""
    stmt = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
