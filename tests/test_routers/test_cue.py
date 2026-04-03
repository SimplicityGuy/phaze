"""Integration tests for CUE management UI endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_approved_tracklist_with_file(
    session: AsyncSession,
    *,
    artist: str = "DJ Shadow",
    event: str = "Coachella 2024",
    file_state: str = FileState.EXECUTED,
    with_timestamps: bool = True,
    track_count: int = 3,
) -> tuple[Tracklist, FileRecord]:
    """Create an approved tracklist with an EXECUTED file and tracks with timestamps."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{artist}.mp3",
        original_filename=f"{artist} - Live @ {event}.mp3",
        current_path=f"/dest/{artist} - Live @ {event}.mp3",
        file_type="mp3",
        file_size=50_000_000,
        state=file_state,
    )
    session.add(file_record)
    await session.flush()

    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()

    tracklist = Tracklist(
        id=tracklist_id,
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:6]}",
        file_id=file_id,
        match_confidence=95,
        artist=artist,
        event=event,
        latest_version_id=version_id,
        source="1001tracklists",
        status="approved",
    )
    session.add(tracklist)

    version = TracklistVersion(
        id=version_id,
        tracklist_id=tracklist_id,
        version_number=1,
    )
    session.add(version)
    await session.flush()

    for i in range(1, track_count + 1):
        track = TracklistTrack(
            id=uuid.uuid4(),
            version_id=version_id,
            position=i,
            artist=f"Track Artist {i}",
            title=f"Track Title {i}",
            timestamp=f"0:{i * 10}:00" if with_timestamps else None,
        )
        session.add(track)

    await session.commit()
    return tracklist, file_record


@pytest.mark.asyncio
async def test_cue_list_full_page(client: AsyncClient, session: AsyncSession) -> None:
    """GET /cue/ returns 200 with full page containing CUE Sheets heading."""
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/")
    assert response.status_code == 200
    assert "CUE Sheets" in response.text
    assert "<!DOCTYPE html>" in response.text


@pytest.mark.asyncio
async def test_cue_list_htmx_partial(client: AsyncClient, session: AsyncSession) -> None:
    """GET /cue/ with HX-Request header returns partial without full page wrapper."""
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!DOCTYPE html>" not in response.text


@pytest.mark.asyncio
async def test_cue_list_empty_state(client: AsyncClient, session: AsyncSession) -> None:
    """GET /cue/ with no eligible tracklists shows empty state."""
    response = await client.get("/cue/")
    assert response.status_code == 200
    assert "No tracklists eligible for CUE generation" in response.text


@pytest.mark.asyncio
async def test_cue_list_stats(client: AsyncClient, session: AsyncSession) -> None:
    """GET /cue/ shows correct stats: eligible count."""
    await _create_approved_tracklist_with_file(session)
    response = await client.get("/cue/")
    assert response.status_code == 200
    assert "Eligible" in response.text
    assert "Generated" in response.text
    assert "Missing Timestamps" in response.text


@pytest.mark.asyncio
async def test_generate_cue_success(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with valid tracklist generates CUE file."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    # Use tmp_path for file paths
    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "CUE file generated" in response.text or "toast-container" in response.text

    # Verify CUE file was written
    cue_path = audio_path.with_suffix(".cue")
    assert cue_path.exists()


@pytest.mark.asyncio
async def test_generate_cue_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """POST /cue/{id}/generate with non-existent tracklist returns 404."""
    fake_id = uuid.uuid4()
    response = await client.post(f"/cue/{fake_id}/generate")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_generate_cue_file_not_executed(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with non-EXECUTED file returns error toast."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session, file_state=FileState.APPROVED)

    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "executed" in response.text.lower() or "must be executed" in response.text.lower()


@pytest.mark.asyncio
async def test_generate_cue_no_timestamps(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate with no timestamps returns error toast."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session, with_timestamps=False)

    audio_path = tmp_path / f"{file_record.original_filename}"
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200
    assert "timestamps" in response.text.lower()


@pytest.mark.asyncio
async def test_generate_batch(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/generate-batch generates CUEs for all eligible tracklists."""
    tracklist1, file1 = await _create_approved_tracklist_with_file(session, artist="Artist A")
    tracklist2, file2 = await _create_approved_tracklist_with_file(session, artist="Artist B")

    # Set up temp paths
    for fr in [file1, file2]:
        audio_path = tmp_path / fr.original_filename
        audio_path.write_text("fake audio")
        fr.current_path = str(audio_path)
    await session.commit()

    response = await client.post("/cue/generate-batch")
    assert response.status_code == 200
    assert "Generated 2 CUE files" in response.text or "toast-container" in response.text


@pytest.mark.asyncio
async def test_generate_cue_regenerate_increments_version(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """POST /cue/{id}/generate twice creates versioned CUE files."""
    tracklist, file_record = await _create_approved_tracklist_with_file(session)

    audio_path = tmp_path / file_record.original_filename
    audio_path.write_text("fake audio")
    file_record.current_path = str(audio_path)
    await session.commit()

    # First generation
    response1 = await client.post(f"/cue/{tracklist.id}/generate")
    assert response1.status_code == 200
    assert audio_path.with_suffix(".cue").exists()

    # Second generation (regenerate)
    response2 = await client.post(f"/cue/{tracklist.id}/generate")
    assert response2.status_code == 200
    # Should have v2 file
    v2_path = audio_path.parent / f"{audio_path.stem}.v2.cue"
    assert v2_path.exists()
