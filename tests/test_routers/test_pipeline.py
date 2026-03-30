"""Tests for the pipeline orchestration router."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.file import FileRecord, FileState


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_file(*, state: str = FileState.DISCOVERED) -> FileRecord:
    """Create a FileRecord with the given state."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


@pytest.mark.asyncio
async def test_dashboard_page(client: AsyncClient) -> None:
    """GET /pipeline/ returns 200 with Pipeline Dashboard heading."""
    response = await client.get("/pipeline/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Pipeline Dashboard" in response.text


@pytest.mark.asyncio
async def test_analyze_enqueues_discovered(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/analyze with DISCOVERED files returns enqueue count > 0."""
    session.add_all([_make_file(state=FileState.DISCOVERED) for _ in range(3)])
    await session.commit()

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock()
    client._transport.app.state.arq_pool = mock_pool  # type: ignore[union-attr]

    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3


@pytest.mark.asyncio
async def test_analyze_no_files(client: AsyncClient) -> None:
    """POST /api/v1/analyze with no DISCOVERED files returns enqueued=0."""
    response = await client.post("/api/v1/analyze")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


@pytest.mark.asyncio
async def test_proposals_generate_batches(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/proposals/generate with ANALYZED files returns batch counts."""
    session.add_all([_make_file(state=FileState.ANALYZED) for _ in range(15)])
    await session.commit()

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock()
    client._transport.app.state.arq_pool = mock_pool  # type: ignore[union-attr]

    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["total_files"] == 15
    assert data["enqueued_batches"] == 2  # 15 files / 10 batch_size = 2 batches


@pytest.mark.asyncio
async def test_proposals_generate_no_files(client: AsyncClient) -> None:
    """POST /api/v1/proposals/generate with no ANALYZED files returns zero counts."""
    response = await client.post("/api/v1/proposals/generate")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued_batches"] == 0
    assert data["total_files"] == 0


@pytest.mark.asyncio
async def test_pipeline_stats_partial(client: AsyncClient, session: AsyncSession) -> None:
    """GET /pipeline/stats returns 200 with HTML containing count values."""
    session.add(_make_file(state=FileState.DISCOVERED))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Stats bar should contain the count
    assert "Discovered" in response.text
    assert "Analyzed" in response.text
