from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.models.proposal import ProposalStatus
from phaze.services.review import get_changes_review_page


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.proposal import RenameProposal


@pytest.mark.asyncio
async def test_changes_review_maps_persisted_states_without_collapsing_them(
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    statuses = [
        ProposalStatus.PENDING,
        ProposalStatus.APPROVED,
        ProposalStatus.EXECUTED,
        ProposalStatus.FAILED,
        ProposalStatus.REJECTED,
    ]
    proposals = []
    for index, status in enumerate(statuses):
        proposal = await seed_pending_proposal(0.91, original_filename=f"state-{index}.mp3")
        proposal.status = status.value
        proposals.append(proposal)
    await session.commit()

    page = await get_changes_review_page(session, status="all", page=1, page_size=25)

    assert page.stats.all == 5
    assert page.stats.needs_review == 1
    assert page.stats.approved == 2
    assert page.stats.blocked == 1
    assert page.stats.rejected == 1
    assert {row["raw_status"] for row in page.rows} == {one.value for one in statuses}


@pytest.mark.asyncio
async def test_changes_review_null_confidence_and_long_values_are_complete(
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    long_name = f"{'long-' * 80}.mp3"
    proposal = await seed_pending_proposal(
        None,
        original_filename="source.mp3",
        proposed_filename=long_name,
        proposed_path=None,
    )

    page = await get_changes_review_page(session, status="needs_review", page=1, page_size=25)

    row = next(one for one in page.rows if one["id"] == proposal.id)
    assert row["proposed_filename"] == long_name
    assert row["proposed_path"] is None
    assert row["bulk_eligible"] is False
    assert row["warnings"] == [
        "Confidence unavailable; individual review required.",
        "No destination change; the file will be renamed in its current directory.",
    ]
