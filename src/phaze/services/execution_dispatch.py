"""Dispatch grouping + revoked-agent filter + chunking helpers (Phase 28 D-09 steps 1-3).

The controller-side helpers that :func:`phaze.routers.execution.start_execution`
(Plan 28-04) calls to convert ``ProposalStatus.APPROVED`` rows into the per-agent,
per-chunk ``ExecuteApprovedBatchPayload`` payloads that flow through
``AgentTaskRouter.enqueue_for_agent``.

Three exports:

- :func:`get_approved_proposals_grouped_by_agent` -- SELECT + GROUP BY
  ``FileRecord.agent_id``, dropping any proposal whose Agent has
  ``revoked_at IS NOT NULL`` (D-09 step 2). Returns
  ``dict[str, list[ExecuteBatchProposalItem]]``.
- :func:`count_revoked_skipped_proposals` -- companion counter. Returns the number
  of APPROVED proposals whose Agent is revoked, so the controller can render the
  ``"Agent X revoked; N proposals skipped"`` banner copy.
- :func:`chunk_proposals` -- pure list-slicing helper that splits a per-agent
  group into sub-lists of length ``<= size`` (D-09 step 3). ``size`` defaults to
  ``_CHUNK_SIZE = 500``, matching the ``Field(max_length=500)`` cap on
  ``ExecuteApprovedBatchPayload.proposals``.

The grouping query uses an explicit JOIN (RenameProposal -> FileRecord -> Agent)
with ``Agent.revoked_at.is_(None)`` filter and ``ORDER BY file.agent_id,
proposal.created_at, proposal.id`` so re-runs produce deterministic chunk
boundaries (downstream callers depend on this for idempotent SAQ enqueues). The
trailing ``proposal.id`` is the mandatory unique tiebreaker -- ``created_at`` is
not unique, so without it a tied block orders arbitrarily (heap order).
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.schemas.agent_tasks import ExecuteBatchProposalItem


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_CHUNK_SIZE = 500
"""Matches ``ExecuteApprovedBatchPayload.proposals`` ``Field(max_length=500)``.

Centralized constant so changing the wire cap requires editing one place.
"""


async def get_approved_proposals_grouped_by_agent(
    session: AsyncSession,
) -> dict[str, list[ExecuteBatchProposalItem]]:
    """Return APPROVED proposals grouped by ``FileRecord.agent_id``.

    Filters out any proposal whose Agent has ``revoked_at IS NOT NULL`` (D-09
    step 2). The companion :func:`count_revoked_skipped_proposals` returns the
    count of those excluded rows so the controller can surface a banner.

    The returned dict's values are ordered by ``RenameProposal.created_at`` ASC,
    then by the unique ``RenameProposal.id`` ASC, so re-runs produce deterministic
    chunk boundaries.

    The ``id`` suffix is load-bearing, not decoration. ``created_at`` is
    ``server_default=func.now()`` and carries no uniqueness constraint, so two
    proposals can share a value; with a partial ORDER BY Postgres may return the
    tied block in ANY order (typically heap order, which shifts with page layout,
    vacuum, and plan choice). Appending the primary key makes the order TOTAL,
    which is what this function's callers actually depend on: the per-agent list
    is the order approved renames are handed to an agent for EXECUTION, and it is
    sliced into batches by :func:`chunk_proposals`. Same rationale as the paging
    contract's mandatory unique tiebreaker (rule 4, see
    :mod:`phaze.services.pagination`) -- and note that contract's explicit
    warning that ``created_at`` is NOT itself a valid tiebreaker in phaze.

    Returns an empty dict when (a) no proposals are ``APPROVED`` OR (b) every
    approved proposal's Agent is revoked.
    """
    stmt = (
        select(RenameProposal, FileRecord)
        .join(FileRecord, RenameProposal.file_id == FileRecord.id)
        .join(Agent, FileRecord.agent_id == Agent.id)
        .where(
            RenameProposal.status == ProposalStatus.APPROVED,
            Agent.revoked_at.is_(None),
        )
        .order_by(FileRecord.agent_id, RenameProposal.created_at, RenameProposal.id)
    )
    result = await session.execute(stmt)

    groups: dict[str, list[ExecuteBatchProposalItem]] = defaultdict(list)
    for proposal, file_record in result.all():
        item = ExecuteBatchProposalItem(
            proposal_id=proposal.id,
            file_id=file_record.id,
            original_path=file_record.original_path,
            # proposed_path is the RELATIVE destination directory; '' (null ->
            # '') tells the executor to rename in place. proposed_filename is
            # always present (non-nullable column) and is what the executor
            # appends under the owning scan_root to build the real destination.
            proposed_path=proposal.proposed_path or "",
            proposed_filename=proposal.proposed_filename,
            sha256_hash=file_record.sha256_hash,
        )
        groups[file_record.agent_id].append(item)
    # Convert defaultdict -> plain dict so callers cannot accidentally mutate
    # by simply reading missing keys.
    return dict(groups)


async def count_revoked_skipped_proposals(session: AsyncSession) -> int:
    """Count APPROVED proposals whose Agent has been revoked.

    Surfaces the N in the controller-rendered banner copy
    ``"Agent X revoked; N proposals skipped"`` (D-09 step 2).
    """
    stmt = (
        select(func.count())
        .select_from(RenameProposal)
        .join(FileRecord, RenameProposal.file_id == FileRecord.id)
        .join(Agent, FileRecord.agent_id == Agent.id)
        .where(
            RenameProposal.status == ProposalStatus.APPROVED,
            Agent.revoked_at.is_not(None),
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one() or 0)


def chunk_proposals(
    items: list[ExecuteBatchProposalItem],
    size: int = _CHUNK_SIZE,
) -> list[list[ExecuteBatchProposalItem]]:
    """Split ``items`` into sub-lists of length ``<= size``.

    Pure / synchronous. ``chunk_proposals([], 500) == []``. For ``N`` items the
    return has ``ceil(N / size)`` chunks where every non-final chunk has length
    exactly ``size``.
    """
    return [items[i : i + size] for i in range(0, len(items), size)]
