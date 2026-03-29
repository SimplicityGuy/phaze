"""Tests for proposal query service — pagination, filtering, stats, status updates."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.proposal_queries import (
    Pagination,
    ProposalStats,
    bulk_update_status,
    get_proposal_stats,
    get_proposal_with_file,
    get_proposals_page,
    update_proposal_status,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_proposal(
    session: AsyncSession,
    *,
    original_filename: str = "test_file.mp3",
    proposed_filename: str = "Artist - Track.mp3",
    confidence: float = 0.85,
    status: str = ProposalStatus.PENDING,
    reason: str = "Test reasoning",
) -> RenameProposal:
    """Create a FileRecord + RenameProposal for testing."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=1_000_000,
        state=FileState.PROPOSAL_GENERATED,
    )
    session.add(file_record)
    await session.flush()

    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename=proposed_filename,
        confidence=confidence,
        status=status,
        context_used={"artist": "Test Artist"},
        reason=reason,
    )
    session.add(proposal)
    await session.commit()
    return proposal


# ---------------------------------------------------------------------------
# Pagination dataclass
# ---------------------------------------------------------------------------


class TestPagination:
    def test_total_pages_with_items(self):
        p = Pagination(page=1, page_size=25, total=60)
        assert p.total_pages == 3

    def test_total_pages_empty(self):
        p = Pagination(page=1, page_size=25, total=0)
        assert p.total_pages == 1

    def test_has_prev_first_page(self):
        p = Pagination(page=1, page_size=25, total=60)
        assert p.has_prev is False

    def test_has_prev_later_page(self):
        p = Pagination(page=2, page_size=25, total=60)
        assert p.has_prev is True

    def test_has_next_last_page(self):
        p = Pagination(page=3, page_size=25, total=60)
        assert p.has_next is False

    def test_has_next_not_last(self):
        p = Pagination(page=1, page_size=25, total=60)
        assert p.has_next is True

    def test_start_and_end(self):
        p = Pagination(page=2, page_size=25, total=60)
        assert p.start == 26
        assert p.end == 50

    def test_start_zero_when_empty(self):
        p = Pagination(page=1, page_size=25, total=0)
        assert p.start == 0

    def test_end_clamped_to_total(self):
        p = Pagination(page=3, page_size=25, total=60)
        assert p.end == 60


# ---------------------------------------------------------------------------
# ProposalStats
# ---------------------------------------------------------------------------


class TestProposalStats:
    def test_dataclass_fields(self):
        stats = ProposalStats(total=10, pending=5, approved=3, rejected=2, avg_confidence=0.75)
        assert stats.total == 10
        assert stats.avg_confidence == 0.75


# ---------------------------------------------------------------------------
# get_proposal_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposal_stats_empty(session: AsyncSession) -> None:
    stats = await get_proposal_stats(session)
    assert stats.total == 0
    assert stats.pending == 0
    assert stats.approved == 0
    assert stats.rejected == 0
    assert stats.avg_confidence is None


@pytest.mark.asyncio
async def test_get_proposal_stats_with_data(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="p1.mp3", status=ProposalStatus.PENDING, confidence=0.80)
    await _create_proposal(session, original_filename="p2.mp3", status=ProposalStatus.APPROVED, confidence=0.90)
    await _create_proposal(session, original_filename="p3.mp3", status=ProposalStatus.REJECTED, confidence=0.60)

    stats = await get_proposal_stats(session)
    assert stats.total == 3
    assert stats.pending == 1
    assert stats.approved == 1
    assert stats.rejected == 1
    assert stats.avg_confidence is not None
    assert abs(stats.avg_confidence - 0.7667) < 0.01


# ---------------------------------------------------------------------------
# get_proposals_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposals_page_default(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="a.mp3")
    proposals, pagination = await get_proposals_page(session)
    assert len(proposals) == 1
    assert pagination.total == 1
    assert pagination.page == 1


@pytest.mark.asyncio
async def test_get_proposals_page_status_filter(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="p.mp3", status=ProposalStatus.PENDING)
    await _create_proposal(session, original_filename="a.mp3", status=ProposalStatus.APPROVED)

    proposals, pagination = await get_proposals_page(session, status="approved")
    assert len(proposals) == 1
    assert pagination.total == 1


@pytest.mark.asyncio
async def test_get_proposals_page_status_all(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="p.mp3", status=ProposalStatus.PENDING)
    await _create_proposal(session, original_filename="a.mp3", status=ProposalStatus.APPROVED)

    proposals, pagination = await get_proposals_page(session, status="all")
    assert len(proposals) == 2
    assert pagination.total == 2


@pytest.mark.asyncio
async def test_get_proposals_page_search(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="coachella_set.mp3", proposed_filename="DJ Shadow - Coachella.mp3")
    await _create_proposal(session, original_filename="other.mp3", proposed_filename="Other Artist.mp3")

    proposals, _pagination = await get_proposals_page(session, status="all", search="coachella")
    assert len(proposals) >= 1
    assert any("Coachella" in p.proposed_filename for p in proposals)


@pytest.mark.asyncio
async def test_get_proposals_page_sort_by_original_filename(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="zzz.mp3", proposed_filename="Z.mp3")
    await _create_proposal(session, original_filename="aaa.mp3", proposed_filename="A.mp3")

    proposals, _ = await get_proposals_page(session, status="all", sort_by="original_filename", sort_order="asc")
    assert len(proposals) == 2
    assert proposals[0].file.original_filename == "aaa.mp3"


@pytest.mark.asyncio
async def test_get_proposals_page_sort_by_proposed_filename(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="x.mp3", proposed_filename="Zebra.mp3")
    await _create_proposal(session, original_filename="y.mp3", proposed_filename="Alpha.mp3")

    proposals, _ = await get_proposals_page(session, status="all", sort_by="proposed_filename", sort_order="asc")
    assert len(proposals) == 2
    assert proposals[0].proposed_filename == "Alpha.mp3"


@pytest.mark.asyncio
async def test_get_proposals_page_sort_desc(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="low.mp3", confidence=0.3)
    await _create_proposal(session, original_filename="high.mp3", confidence=0.9)

    proposals, _ = await get_proposals_page(session, status="all", sort_by="confidence", sort_order="desc")
    assert proposals[0].confidence > proposals[1].confidence


@pytest.mark.asyncio
async def test_get_proposals_page_invalid_sort_falls_back(session: AsyncSession) -> None:
    await _create_proposal(session, original_filename="a.mp3")

    proposals, _ = await get_proposals_page(session, status="all", sort_by="nonexistent")
    assert len(proposals) == 1  # falls back to confidence sort without error


@pytest.mark.asyncio
async def test_get_proposals_page_pagination(session: AsyncSession) -> None:
    for i in range(5):
        await _create_proposal(session, original_filename=f"f{i}.mp3", proposed_filename=f"P{i}.mp3")

    proposals, pagination = await get_proposals_page(session, status="all", page=2, page_size=2)
    assert len(proposals) == 2
    assert pagination.page == 2
    assert pagination.total == 5
    assert pagination.total_pages == 3


# ---------------------------------------------------------------------------
# update_proposal_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_proposal_status_approve(session: AsyncSession) -> None:
    proposal = await _create_proposal(session)
    result = await update_proposal_status(session, proposal.id, ProposalStatus.APPROVED)
    assert result is not None
    assert result.status == ProposalStatus.APPROVED
    assert result.file is not None


@pytest.mark.asyncio
async def test_update_proposal_status_not_found(session: AsyncSession) -> None:
    result = await update_proposal_status(session, uuid.uuid4(), ProposalStatus.APPROVED)
    assert result is None


# ---------------------------------------------------------------------------
# bulk_update_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_status(session: AsyncSession) -> None:
    p1 = await _create_proposal(session, original_filename="b1.mp3")
    p2 = await _create_proposal(session, original_filename="b2.mp3")

    count = await bulk_update_status(session, [p1.id, p2.id], ProposalStatus.REJECTED)
    assert count == 2

    updated1 = await session.get(RenameProposal, p1.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_bulk_update_status_empty_list(session: AsyncSession) -> None:
    count = await bulk_update_status(session, [], ProposalStatus.APPROVED)
    assert count == 0


# ---------------------------------------------------------------------------
# get_proposal_with_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposal_with_file(session: AsyncSession) -> None:
    proposal = await _create_proposal(session)
    result = await get_proposal_with_file(session, proposal.id)
    assert result is not None
    assert result.file is not None
    assert result.file.original_filename == "test_file.mp3"


@pytest.mark.asyncio
async def test_get_proposal_with_file_not_found(session: AsyncSession) -> None:
    result = await get_proposal_with_file(session, uuid.uuid4())
    assert result is None
