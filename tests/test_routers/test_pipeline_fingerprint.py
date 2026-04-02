"""Tests for fingerprint pipeline endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_file(*, state: str = FileState.METADATA_EXTRACTED) -> FileRecord:
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
async def test_trigger_fingerprint_enqueues_eligible(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/fingerprint with METADATA_EXTRACTED files returns enqueue count."""
    session.add_all([_make_file(state=FileState.METADATA_EXTRACTED) for _ in range(3)])
    await session.commit()

    mock_queue = AsyncMock()
    mock_queue.enqueue = AsyncMock()
    client._transport.app.state.queue = mock_queue  # type: ignore[union-attr]

    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 3


@pytest.mark.asyncio
async def test_trigger_fingerprint_no_eligible(client: AsyncClient) -> None:
    """POST /api/v1/fingerprint returns 0 when no eligible files exist."""
    response = await client.post("/api/v1/fingerprint")
    assert response.status_code == 200
    data = response.json()
    assert data["enqueued"] == 0


@pytest.mark.asyncio
async def test_fingerprint_progress_returns_counts(client: AsyncClient, session: AsyncSession) -> None:
    """GET /api/v1/fingerprint/progress returns total/completed/failed counts."""
    # 2 files in METADATA_EXTRACTED (eligible, not yet done)
    f1 = _make_file(state=FileState.METADATA_EXTRACTED)
    f2 = _make_file(state=FileState.METADATA_EXTRACTED)
    # 1 file in FINGERPRINTED (completed)
    f3 = _make_file(state=FileState.FINGERPRINTED)
    session.add_all([f1, f2, f3])
    await session.flush()

    # Add a failed fingerprint result
    session.add(FingerprintResult(file_id=f1.id, engine="audfprint", status="failed", error_message="timeout"))
    await session.commit()

    response = await client.get("/api/v1/fingerprint/progress")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert data["completed"] == 1
    assert data["failed"] == 1


@pytest.mark.asyncio
async def test_pipeline_stats_include_fingerprinted(client: AsyncClient, session: AsyncSession) -> None:
    """Pipeline stats include FINGERPRINTED stage."""
    session.add(_make_file(state=FileState.FINGERPRINTED))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    assert "Fingerprinted" in response.text
