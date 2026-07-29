"""Tests for the Rename/Move pending-proposal totals bundle (phaze-rw14).

``get_pending_proposal_rows`` used to return a bare, 200-row-capped list, and the Rename/Move
workspace templates quoted ``rename_proposals | length`` / ``move_proposals | length`` for both
the header sub-count AND the bulk-approve confirm dialog's match count -- so a backlog past 200
silently reported exactly 200, forever, with no pager and no truncation notice, and the confirm
dialog quoted the render cap instead of the real >=90%-confidence predicate count. These tests
assert the fixed contract: the header total and the confidence-match count are REAL corpus-wide
counts, independent of how many rows the render actually returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.review import PendingProposalRows, get_pending_proposal_rows


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _bulk_seed_pending(session: AsyncSession, count: int, *, confidence: float) -> None:
    """Insert ``count`` PENDING proposals (one FileRecord each) at a fixed confidence, in one flush.

    Bypasses the one-commit-per-row ``seed_pending_proposal`` fixture -- this module seeds
    hundreds of rows per test to exercise the 200-row render cap, and a single ``flush`` keeps
    that cheap.
    """
    for i in range(count):
        file_id = uuid.uuid4()
        session.add(
            FileRecord(
                agent_id="test-fileserver",
                id=file_id,
                sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                original_path=f"/music/{uuid.uuid4().hex}/bulk_{i}.mp3",
                original_filename=f"bulk_{i}.mp3",
                current_path=f"/music/bulk_{i}.mp3",
                file_type="music",
                file_size=1_000,
            )
        )
        session.add(
            RenameProposal(
                id=uuid.uuid4(),
                file_id=file_id,
                proposed_filename=f"Bulk {i}.mp3",
                confidence=confidence,
                status=ProposalStatus.PENDING.value,
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_get_pending_proposal_rows_totals_match_len_under_the_cap(session: AsyncSession) -> None:
    """Under the 200-row cap, total_pending and high_confidence_pending are real counts that
    happen to agree with the rendered rows -- the bundle isn't just echoing len(rows).
    """
    await _bulk_seed_pending(session, 3, confidence=0.95)
    await _bulk_seed_pending(session, 2, confidence=0.5)

    result = await get_pending_proposal_rows(session, confidence_threshold=0.9)
    assert isinstance(result, PendingProposalRows)
    assert len(result.rows) == 5
    assert result.total_pending == 5
    assert result.high_confidence_pending == 3


@pytest.mark.asyncio
async def test_get_pending_proposal_rows_total_exceeds_capped_render(session: AsyncSession) -> None:
    """phaze-rw14 core regression: past the 200-row render cap, total_pending is the TRUE corpus
    count, not the length of the capped row list -- the exact defect where the header used to
    report "200 awaiting approval" no matter how large the backlog actually was.
    """
    await _bulk_seed_pending(session, 205, confidence=0.5)

    result = await get_pending_proposal_rows(session)
    assert len(result.rows) == 200, "the row list stays capped at the render bound"
    assert result.total_pending == 205, "the total must be the real count, not len(rows)"


@pytest.mark.asyncio
async def test_get_pending_proposal_rows_high_confidence_count_ignores_render_order(session: AsyncSession) -> None:
    """phaze-rw14 second regression: rows render confidence-ASC, so the visible page can be
    entirely LOW-confidence while high-confidence rows are approvable server-side. The bulk-approve
    confirm dialog's count must reflect that, not the (here: zero) high-confidence rows on the
    rendered page.
    """
    # 205 low-confidence rows sort first (confidence ASC) and fill the entire 200-row render cap;
    # the 10 high-confidence rows are entirely on the hidden tail.
    await _bulk_seed_pending(session, 205, confidence=0.5)
    await _bulk_seed_pending(session, 10, confidence=0.95)

    result = await get_pending_proposal_rows(session, confidence_threshold=0.9)
    assert all(row["confidence"] == 0.5 for row in result.rows), "the rendered page is entirely low-confidence rows"
    assert result.high_confidence_pending == 10, "the real match count still counts the hidden high-confidence tail"
    assert result.total_pending == 215
