"""Tests for the companion association and duplicate detection API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _file(path: str, sha: str) -> FileRecord:
    return FileRecord(
        agent_id="test-fileserver",
        sha256_hash=sha,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type=path.rsplit(".", 1)[-1],
        file_size=1000,
    )


def _features(companion: FileRecord, *, junk_class: str | None = None) -> CompanionContentFeatures:
    """Current stored features (phaze-rmhfr: association decides only companions that carry them)."""
    return CompanionContentFeatures(
        file_id=companion.id,
        agent_id=companion.agent_id,
        fingerprint=companion.sha256_hash,
        encoding="ascii",
        byte_size=100,
        is_tracklist=False,
        content_junk_class=junk_class,
        junk_class=junk_class,
        extractor_version=1,
    )


async def _seed_release(session: AsyncSession, folder: str, media_sha: str, companion_sha: str) -> None:
    media = _file(f"{folder}/track1.mp3", media_sha)
    companion = _file(f"{folder}/release.nfo", companion_sha)
    session.add_all([media, companion])
    await session.flush()
    session.add(_features(companion))
    await session.commit()


@pytest.mark.asyncio
async def test_associate_creates_links(client: AsyncClient, session: AsyncSession) -> None:
    """POST /api/v1/associate runs the linking chain: a release note links to the media beside it."""
    await _seed_release(session, "/music/album", "a" * 64, "b" * 64)

    response = await client.post("/api/v1/associate")

    assert response.status_code == 200
    data = response.json()
    assert (data["new_associations"], data["removed_associations"], data["awaiting_features"]) == (1, 0, 0)
    assert data["message"] == "Associated 1 companion link(s) with media files"


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
    await _seed_release(session, "/music/album2", "c" * 64, "d" * 64)

    first_response = await client.post("/api/v1/associate")
    assert first_response.status_code == 200
    assert first_response.json()["new_associations"] == 1

    second_response = await client.post("/api/v1/associate")
    assert second_response.status_code == 200
    assert second_response.json()["new_associations"] == 0


@pytest.mark.asyncio
async def test_associate_reports_removed_junk_links_and_companions_awaiting_features(client: AsyncClient, session: AsyncSession) -> None:
    """The response says what the run removed and what it could not decide yet, never just a bare zero."""
    media = _file("/music/album3/track.mp3", "e" * 64)
    stamp = _file("/music/album3/site.nfo", "f" * 64)
    unread = _file("/music/album3/notes.txt", "0" * 64)
    session.add_all([media, stamp, unread])
    await session.flush()
    session.add(_features(stamp, junk_class="empty"))
    session.add(FileCompanion(companion_id=stamp.id, media_id=media.id))
    await session.commit()

    response = await client.post("/api/v1/associate")

    data = response.json()
    assert (data["new_associations"], data["removed_associations"], data["awaiting_features"]) == (0, 1, 1)
    assert data["message"] == (
        "Associated 0 companion link(s) with media files; removed 1 link(s) held by junk companions; "
        "1 companion(s) await content features (phaze backfill companion-features)"
    )


@pytest.mark.asyncio
async def test_duplicates_returns_groups(client: AsyncClient, session: AsyncSession) -> None:
    """GET /api/v1/duplicates should return duplicate groups when files share sha256 hashes."""
    dup_hash = "d" * 64
    file1 = FileRecord(
        agent_id="test-fileserver",
        sha256_hash=dup_hash,
        original_path="/music/dir1/track.mp3",
        original_filename="track.mp3",
        current_path="/music/dir1/track.mp3",
        file_type="mp3",
        file_size=3000000,
    )
    file2 = FileRecord(
        agent_id="test-fileserver",
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
    session.add_all(
        [
            FileRecord(
                agent_id="test-fileserver",
                sha256_hash=hash_a,
                original_path="/music/g1/file1.mp3",
                original_filename="file1.mp3",
                current_path="/music/g1/file1.mp3",
                file_type="mp3",
                file_size=1000000,
            ),
            FileRecord(
                agent_id="test-fileserver",
                sha256_hash=hash_a,
                original_path="/music/g1/file1_dup.mp3",
                original_filename="file1_dup.mp3",
                current_path="/music/g1/file1_dup.mp3",
                file_type="mp3",
                file_size=1000000,
            ),
        ]
    )
    # Group 2: two files with different same hash
    hash_b = "b" * 64
    session.add_all(
        [
            FileRecord(
                agent_id="test-fileserver",
                sha256_hash=hash_b,
                original_path="/music/g2/file2.mp3",
                original_filename="file2.mp3",
                current_path="/music/g2/file2.mp3",
                file_type="mp3",
                file_size=2000000,
            ),
            FileRecord(
                agent_id="test-fileserver",
                sha256_hash=hash_b,
                original_path="/music/g2/file2_dup.mp3",
                original_filename="file2_dup.mp3",
                current_path="/music/g2/file2_dup.mp3",
                file_type="mp3",
                file_size=2000000,
            ),
        ]
    )
    await session.commit()

    response = await client.get("/api/v1/duplicates?limit=1")

    assert response.status_code == 200
    data = response.json()
    assert len(data["groups"]) == 1
    assert data["total_groups"] == 2
    assert data["limit"] == 1
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_duplicates_negative_limit_returns_422_not_500(client: AsyncClient) -> None:
    """phaze-hpo9: a negative limit is a clean 422, never an unhandled 500.

    Before the fix, ``limit``/``offset`` were bare ``int`` defaults with no ``Query`` guard, so
    ``limit=-1`` reached ``.limit(limit)`` on the dup_hashes subquery and Postgres rejected it
    with "LIMIT must not be negative", surfacing as an unhandled 500. This route builds its own
    raw limit/offset pair (it does not go through ``phaze.services.pagination``), so wire_bounds
    rule 8's last clause applies: it needs its own ``ge=`` guard.
    """
    response = await client.get("/api/v1/duplicates?limit=-1")
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_duplicates_negative_offset_returns_422_not_500(client: AsyncClient) -> None:
    """phaze-hpo9: a negative offset is a clean 422, never an unhandled 500 (see limit sibling test)."""
    response = await client.get("/api/v1/duplicates?offset=-1")
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_duplicates_zero_and_valid_limit_offset_still_work(client: AsyncClient) -> None:
    """Non-negative limit/offset within bounds are unaffected by the new guard (belt-and-braces)."""
    response = await client.get("/api/v1/duplicates?limit=10&offset=0")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 10
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
