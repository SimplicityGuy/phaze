"""Tests for the companion association and duplicate detection API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.models.file import FileRecord


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_associate_creates_links(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/associate should create links between companion and media files in same directory."""
    media = FileRecord(
        sha256_hash="a" * 64,
        original_path="/music/album/track1.mp3",
        original_filename="track1.mp3",
        current_path="/music/album/track1.mp3",
        file_type="mp3",
        file_size=5000000,
    )
    companion = FileRecord(
        sha256_hash="b" * 64,
        original_path="/music/album/cover.jpg",
        original_filename="cover.jpg",
        current_path="/music/album/cover.jpg",
        file_type="jpg",
        file_size=100000,
    )
    session.add_all([media, companion])
    await session.commit()

    response = await client.post("/api/v1/associate")

    assert response.status_code == 200
    data = response.json()
    assert data["new_associations"] >= 1
    assert "message" in data


@pytest.mark.asyncio
async def test_associate_no_unlinked(client: AsyncClient) -> None:
    """POST /api/v1/associate with no data should return new_associations=0."""
    response = await client.post("/api/v1/associate")

    assert response.status_code == 200
    data = response.json()
    assert data["new_associations"] == 0


@pytest.mark.asyncio
async def test_associate_idempotent(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/associate called twice should return 0 on the second call."""
    media = FileRecord(
        sha256_hash="c" * 64,
        original_path="/music/album2/song.mp3",
        original_filename="song.mp3",
        current_path="/music/album2/song.mp3",
        file_type="mp3",
        file_size=4000000,
    )
    companion = FileRecord(
        sha256_hash="d" * 64,
        original_path="/music/album2/artwork.jpg",
        original_filename="artwork.jpg",
        current_path="/music/album2/artwork.jpg",
        file_type="jpg",
        file_size=50000,
    )
    session.add_all([media, companion])
    await session.commit()

    # First call creates links
    first_response = await client.post("/api/v1/associate")
    assert first_response.status_code == 200
    assert first_response.json()["new_associations"] >= 1

    # Second call should find nothing new
    second_response = await client.post("/api/v1/associate")
    assert second_response.status_code == 200
    assert second_response.json()["new_associations"] == 0


@pytest.mark.asyncio
async def test_duplicates_returns_groups(client: AsyncClient, session: AsyncSession) -> None:
    """GET /api/v1/duplicates should return duplicate groups when files share sha256 hashes."""
    dup_hash = "d" * 64
    file1 = FileRecord(
        sha256_hash=dup_hash,
        original_path="/music/dir1/track.mp3",
        original_filename="track.mp3",
        current_path="/music/dir1/track.mp3",
        file_type="mp3",
        file_size=3000000,
    )
    file2 = FileRecord(
        sha256_hash=dup_hash,
        original_path="/music/dir2/track_copy.mp3",
        original_filename="track_copy.mp3",
        current_path="/music/dir2/track_copy.mp3",
        file_type="mp3",
        file_size=3000000,
    )
    session.add_all([file1, file2])
    await session.commit()

    response = await client.get("/api/v1/duplicates")

    assert response.status_code == 200
    data = response.json()
    assert data["total_groups"] >= 1
    group = data["groups"][0]
    assert group["count"] >= 2
    assert "sha256_hash" in group
    assert "files" in group
    assert len(group["files"]) >= 2


@pytest.mark.asyncio
async def test_duplicates_empty(client: AsyncClient) -> None:
    """GET /api/v1/duplicates with no data should return empty groups."""
    response = await client.get("/api/v1/duplicates")

    assert response.status_code == 200
    data = response.json()
    assert data["groups"] == []
    assert data["total_groups"] == 0


@pytest.mark.asyncio
async def test_duplicates_pagination(client: AsyncClient, session: AsyncSession) -> None:
    """GET /api/v1/duplicates with limit=1 should return 1 group with correct total."""
    # Group 1: two files with same hash
    hash_a = "a" * 64
    session.add_all([
        FileRecord(
            sha256_hash=hash_a,
            original_path="/music/g1/file1.mp3",
            original_filename="file1.mp3",
            current_path="/music/g1/file1.mp3",
            file_type="mp3",
            file_size=1000000,
        ),
        FileRecord(
            sha256_hash=hash_a,
            original_path="/music/g1/file1_dup.mp3",
            original_filename="file1_dup.mp3",
            current_path="/music/g1/file1_dup.mp3",
            file_type="mp3",
            file_size=1000000,
        ),
    ])
    # Group 2: two files with different same hash
    hash_b = "b" * 64
    session.add_all([
        FileRecord(
            sha256_hash=hash_b,
            original_path="/music/g2/file2.mp3",
            original_filename="file2.mp3",
            current_path="/music/g2/file2.mp3",
            file_type="mp3",
            file_size=2000000,
        ),
        FileRecord(
            sha256_hash=hash_b,
            original_path="/music/g2/file2_dup.mp3",
            original_filename="file2_dup.mp3",
            current_path="/music/g2/file2_dup.mp3",
            file_type="mp3",
            file_size=2000000,
        ),
    ])
    await session.commit()

    response = await client.get("/api/v1/duplicates?limit=1")

    assert response.status_code == 200
    data = response.json()
    assert len(data["groups"]) == 1
    assert data["total_groups"] == 2
    assert data["limit"] == 1
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_duplicates_response_shape(client: AsyncClient) -> None:
    """GET /api/v1/duplicates should return response matching DuplicateGroupsResponse schema."""
    response = await client.get("/api/v1/duplicates")

    assert response.status_code == 200
    data = response.json()
    assert "groups" in data
    assert "total_groups" in data
    assert "limit" in data
    assert "offset" in data
    assert isinstance(data["groups"], list)
    assert isinstance(data["total_groups"], int)
    assert isinstance(data["limit"], int)
    assert isinstance(data["offset"], int)
