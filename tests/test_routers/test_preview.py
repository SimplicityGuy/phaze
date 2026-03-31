"""Integration tests for the preview route -- directory tree preview page."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_approved_proposal(
    session: AsyncSession,
    *,
    proposed_filename: str = "Artist - Track.mp3",
    proposed_path: str | None = "performances/artists/Disclosure",
    original_filename: str | None = None,
) -> RenameProposal:
    """Create an approved proposal with its associated file record."""
    file_id = uuid.uuid4()
    fname = original_filename or f"{uuid.uuid4().hex[:8]}.mp3"
    file_record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{fname}",
        original_filename=fname,
        current_path=f"/music/{fname}",
        file_type="music",
        file_size=1_000_000,
        state=FileState.APPROVED,
    )
    session.add(file_record)
    await session.flush()

    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename=proposed_filename,
        proposed_path=proposed_path,
        confidence=0.9,
        status=ProposalStatus.APPROVED,
        context_used={"artist": "Test"},
        reason="test",
    )
    session.add(proposal)
    await session.commit()
    return proposal


@pytest.mark.asyncio
async def test_preview_returns_200_with_heading(client: AsyncClient) -> None:
    """GET /preview/ returns 200 with Directory Preview heading."""
    response = await client.get("/preview/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Directory Preview" in response.text


@pytest.mark.asyncio
async def test_preview_empty_state(client: AsyncClient) -> None:
    """GET /preview/ with no approved proposals shows empty state."""
    response = await client.get("/preview/")
    assert response.status_code == 200
    assert "No approved proposals" in response.text


@pytest.mark.asyncio
async def test_preview_renders_tree(client: AsyncClient, session: AsyncSession) -> None:
    """GET /preview/ with approved proposals renders tree structure."""
    await _create_approved_proposal(
        session,
        proposed_filename="Live.mp3",
        proposed_path="performances/artists/Disclosure",
    )
    response = await client.get("/preview/")
    assert response.status_code == 200
    assert "Disclosure" in response.text
    assert "performances" in response.text
    assert "Expand All" in response.text
    assert "Collapse All" in response.text
    assert "tree-container" in response.text
