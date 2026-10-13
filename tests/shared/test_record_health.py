"""Record processing evidence must never be inferred from the proposal-review count."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from bs4 import BeautifulSoup
import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["record", "files"])
@pytest.mark.parametrize("state", ["failed", "missing", "incomplete", "complete", "pending_failed"])
async def test_record_separates_health_from_review(client: AsyncClient, session: AsyncSession, route: str, state: str) -> None:
    """Both real presentations show failure even when proposals are absent or pending."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id="test-fileserver",
            sha256_hash=uuid.uuid4().hex * 2,
            original_path="/test/music/song.mp3",
            current_path="/test/music/song.mp3",
            original_filename="song.mp3",
            file_type="mp3",
            file_size=1024,
            missing_at=datetime.now(UTC) if state == "missing" else None,
        )
    )
    await session.flush()
    if state in {"failed", "pending_failed", "missing"}:
        session.add(AnalysisResult(file_id=file_id, failed_at=datetime.now(UTC), error_message="ffprobe: <source> unavailable"))
    elif state == "complete":
        session.add(FileMetadata(file_id=file_id))
        session.add(AnalysisResult(file_id=file_id, analysis_completed_at=datetime.now(UTC)))
    if state == "pending_failed":
        session.add(
            RenameProposal(
                file_id=file_id,
                proposed_filename="Artist - Title.mp3",
                proposed_path="/organized/Artist - Title.mp3",
                confidence=0.9,
                status=ProposalStatus.PENDING.value,
            )
        )
    await session.commit()

    response = await client.get(f"/{route}/{file_id}")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    health = page.select_one("[data-record-health]")
    review = page.select_one("[data-review-banner]")
    assert health is not None and review is not None
    assert health["data-record-health"] == ("failed" if state == "pending_failed" else state)
    assert "up to date" not in response.text
    assert "text-ok" not in str(review)
    assert review["data-review-state"] == ("pending" if state == "pending_failed" else "clear")
    assert ("No proposed changes awaiting review" in review.get_text()) == (state != "pending_failed")
    content = page.select_one("[data-record-content]")
    assert content is not None and content.find(recursive=False) == health
    if state in {"failed", "pending_failed", "missing"}:
        link = health.select_one("a")
        assert link is not None and link["href"] == f"#record-stage-eligibility-{file_id}"
        assert page.select_one(f"#record-stage-eligibility-{file_id}") is not None
        assert "ffprobe: <source> unavailable" in page.get_text()
        assert "ffprobe: &lt;source&gt; unavailable" in response.text
    if state == "complete":
        assert "Metadata and analysis complete" in health.get_text()
    if state == "missing":
        assert "Source file is missing" in health.get_text()
