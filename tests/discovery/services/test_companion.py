"""Tests for companion association service."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.file import FileRecord, FileState
from phaze.models.file_companion import FileCompanion
from phaze.services.companion import associate_companions


def _make_file(
    original_path: str,
    file_type: str,
    sha256_hash: str | None = None,
    file_size: int = 1000,
) -> FileRecord:
    """Helper to create a FileRecord with sensible defaults."""
    if sha256_hash is None:
        sha256_hash = uuid.uuid4().hex + uuid.uuid4().hex[:32]  # 64 hex chars
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=file_size,
        state=FileState.DISCOVERED,
    )


@pytest.mark.asyncio
async def test_companion_links_to_media_in_same_dir(session: AsyncSession) -> None:
    """Companion in same dir as 2 media files creates 2 FileCompanion rows."""
    media1 = _make_file("/music/album/track1.mp3", "mp3")
    media2 = _make_file("/music/album/track2.flac", "flac")
    companion = _make_file("/music/album/cover.jpg", "jpg")

    session.add_all([media1, media2, companion])
    await session.flush()

    count = await associate_companions(session)

    assert count == 2
    result = await session.execute(select(FileCompanion))
    links = result.scalars().all()
    assert len(links) == 2
    linked_media_ids = {link.media_id for link in links}
    assert linked_media_ids == {media1.id, media2.id}
    assert all(link.companion_id == companion.id for link in links)


@pytest.mark.asyncio
async def test_companion_no_media_in_dir(session: AsyncSession) -> None:
    """Companion in dir with no media files creates 0 rows."""
    companion = _make_file("/docs/readme.txt", "txt")
    session.add(companion)
    await session.flush()

    count = await associate_companions(session)

    assert count == 0
    result = await session.execute(select(FileCompanion))
    assert len(result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_idempotent_association(session: AsyncSession) -> None:
    """Running association twice does not create duplicate rows."""
    media = _make_file("/music/album/track.mp3", "mp3")
    companion = _make_file("/music/album/cover.jpg", "jpg")
    session.add_all([media, companion])
    await session.flush()

    count1 = await associate_companions(session)
    count2 = await associate_companions(session)

    assert count1 == 1
    assert count2 == 0
    result = await session.execute(select(FileCompanion))
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_already_linked_skipped(session: AsyncSession) -> None:
    """Already-linked companions are skipped on re-run."""
    media = _make_file("/music/album/song.m4a", "m4a")
    comp1 = _make_file("/music/album/cover.jpg", "jpg")
    comp2 = _make_file("/music/album/info.nfo", "nfo")
    session.add_all([media, comp1, comp2])
    await session.flush()

    count1 = await associate_companions(session)
    assert count1 == 2

    # Add a new companion, re-run -- only new one gets linked
    comp3 = _make_file("/music/album/tracklist.txt", "txt")
    session.add(comp3)
    await session.flush()

    count2 = await associate_companions(session)
    assert count2 == 1

    result = await session.execute(select(FileCompanion))
    assert len(result.scalars().all()) == 3


@pytest.mark.asyncio
async def test_companions_link_only_to_own_dir(session: AsyncSession) -> None:
    """Companions in different dirs link only to media in their own dir."""
    media_a = _make_file("/music/albumA/track.mp3", "mp3")
    comp_a = _make_file("/music/albumA/cover.jpg", "jpg")
    media_b = _make_file("/music/albumB/song.ogg", "ogg")
    comp_b = _make_file("/music/albumB/art.png", "png")
    session.add_all([media_a, comp_a, media_b, comp_b])
    await session.flush()

    count = await associate_companions(session)

    assert count == 2
    result = await session.execute(select(FileCompanion).order_by(FileCompanion.companion_id))
    links = result.scalars().all()
    link_pairs = {(link.companion_id, link.media_id) for link in links}
    assert (comp_a.id, media_a.id) in link_pairs
    assert (comp_b.id, media_b.id) in link_pairs
    # No cross-directory links
    assert (comp_a.id, media_b.id) not in link_pairs
    assert (comp_b.id, media_a.id) not in link_pairs
