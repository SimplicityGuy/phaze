"""Integration tests for proposal approval workflow UI endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_test_proposal(
    session: AsyncSession,
    *,
    original_filename: str = "test_file.mp3",
    proposed_filename: str = "Artist - Track.mp3",
    confidence: float = 0.85,
    status: str = ProposalStatus.PENDING,
    reason: str = "Test reasoning",
) -> RenameProposal:
    """Create a FileRecord + RenameProposal pair for testing."""
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
        context_used={"artist": "Test Artist", "event_name": "Test Event"},
        reason=reason,
    )
    session.add(proposal)
    await session.commit()
    return proposal


@pytest.mark.asyncio
async def test_proposals_list_returns_html(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/ returns 200 with text/html content type."""
    await create_test_proposal(session)
    response = await client.get("/proposals/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Proposal Review" in response.text


@pytest.mark.asyncio
async def test_proposals_list_empty_state(client: AsyncClient) -> None:
    """GET /proposals/ with no proposals returns 200 with empty state message."""
    response = await client.get("/proposals/")
    assert response.status_code == 200
    assert "No proposals yet" in response.text


@pytest.mark.asyncio
async def test_proposals_list_shows_proposals(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/ with seeded proposals returns rows containing proposed_filename text."""
    await create_test_proposal(session, proposed_filename="DJ Shadow - Live @ Coachella 2025.mp3")
    response = await client.get("/proposals/")
    assert response.status_code == 200
    assert "DJ Shadow - Live @ Coachella 2025.mp3" in response.text


@pytest.mark.asyncio
async def test_proposals_htmx_returns_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/ with HX-Request header returns fragment without full HTML page."""
    await create_test_proposal(session)
    response = await client.get("/proposals/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text.lower()
    assert "<table" in response.text.lower() or "<tbody" in response.text.lower()


@pytest.mark.asyncio
async def test_proposals_pagination(client: AsyncClient, session: AsyncSession) -> None:
    """Seed 60 proposals, GET /proposals/?page=2&page_size=25 returns correct range text."""
    for i in range(60):
        await create_test_proposal(
            session,
            original_filename=f"file_{i:03d}.mp3",
            proposed_filename=f"Artist - Track {i:03d}.mp3",
        )
    response = await client.get("/proposals/?status=all&page=2&page_size=25")
    assert response.status_code == 200
    assert "Showing 26-50" in response.text


@pytest.mark.asyncio
async def test_filter_by_status(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/?status=approved returns only approved proposals."""
    await create_test_proposal(session, proposed_filename="Pending One.mp3", status=ProposalStatus.PENDING, original_filename="p1.mp3")
    await create_test_proposal(session, proposed_filename="Approved One.mp3", status=ProposalStatus.APPROVED, original_filename="a1.mp3")
    await create_test_proposal(session, proposed_filename="Rejected One.mp3", status=ProposalStatus.REJECTED, original_filename="r1.mp3")

    response = await client.get("/proposals/?status=approved")
    assert response.status_code == 200
    assert "Approved One.mp3" in response.text
    assert "Pending One.mp3" not in response.text
    assert "Rejected One.mp3" not in response.text


@pytest.mark.asyncio
async def test_search_proposals(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/?q=searchterm returns matching proposals."""
    await create_test_proposal(
        session,
        original_filename="coachella_set.mp3",
        proposed_filename="DJ Shadow - Live @ Coachella.mp3",
    )
    await create_test_proposal(
        session,
        original_filename="random_track.mp3",
        proposed_filename="Random Artist - Song.mp3",
    )
    response = await client.get("/proposals/?status=all&q=coachella")
    assert response.status_code == 200
    assert "Coachella" in response.text


@pytest.mark.asyncio
async def test_approve_proposal(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/{id}/approve returns 200, updates DB status, includes stats-bar."""
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/approve")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "stats-bar" in response.text

    # Verify DB state
    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_reject_proposal(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/{id}/reject returns 200, updates DB status to rejected."""
    proposal = await create_test_proposal(session)
    response = await client.patch(f"/proposals/{proposal.id}/reject")
    assert response.status_code == 200

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_undo_proposal(client: AsyncClient, session: AsyncSession) -> None:
    """Approve then undo -- proposal should revert to pending."""
    proposal = await create_test_proposal(session, status=ProposalStatus.APPROVED)
    response = await client.patch(f"/proposals/{proposal.id}/undo")
    assert response.status_code == 200

    updated = await session.get(RenameProposal, proposal.id)
    assert updated is not None
    assert updated.status == ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_approve_not_found(client: AsyncClient) -> None:
    """PATCH /proposals/{random_uuid}/approve returns 404."""
    random_id = uuid.uuid4()
    response = await client.patch(f"/proposals/{random_id}/approve")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_row_detail(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/{id}/detail returns 200 with AI Reasoning text."""
    proposal = await create_test_proposal(session, reason="Because the artist name matches Coachella lineup")
    response = await client.get(f"/proposals/{proposal.id}/detail")
    assert response.status_code == 200
    assert "AI Reasoning" in response.text
    assert "Because the artist name matches Coachella lineup" in response.text


@pytest.mark.asyncio
async def test_bulk_approve(client: AsyncClient, session: AsyncSession) -> None:
    """PATCH /proposals/bulk with action=approve updates all listed proposals."""
    p1 = await create_test_proposal(session, original_filename="bulk1.mp3", proposed_filename="Bulk One.mp3")
    p2 = await create_test_proposal(session, original_filename="bulk2.mp3", proposed_filename="Bulk Two.mp3")

    response = await client.patch(
        "/proposals/bulk",
        data={"action": "approve", "proposal_ids": [str(p1.id), str(p2.id)]},
    )
    assert response.status_code == 200

    updated1 = await session.get(RenameProposal, p1.id)
    updated2 = await session.get(RenameProposal, p2.id)
    assert updated1 is not None
    assert updated1.status == ProposalStatus.APPROVED
    assert updated2 is not None
    assert updated2.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_sort_by_confidence(client: AsyncClient, session: AsyncSession) -> None:
    """GET /proposals/?sort=confidence&order=asc returns proposals sorted by confidence ascending."""
    await create_test_proposal(session, original_filename="high.mp3", proposed_filename="High Conf.mp3", confidence=0.95)
    await create_test_proposal(session, original_filename="low.mp3", proposed_filename="Low Conf.mp3", confidence=0.30)
    await create_test_proposal(session, original_filename="mid.mp3", proposed_filename="Mid Conf.mp3", confidence=0.60)

    response = await client.get("/proposals/?status=all&sort=confidence&order=asc")
    assert response.status_code == 200
    text = response.text
    # Low confidence should appear before high confidence in ascending order
    low_pos = text.find("Low Conf.mp3")
    mid_pos = text.find("Mid Conf.mp3")
    high_pos = text.find("High Conf.mp3")
    assert low_pos < mid_pos < high_pos
