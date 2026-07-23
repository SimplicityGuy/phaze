"""Tests for companion association service."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services.companion import associate_companions


def _make_file(
    original_path: str,
    file_type: str,
    sha256_hash: str | None = None,
    file_size: int = 1000,
    agent_id: str = "test-fileserver",
) -> FileRecord:
    """Helper to create a FileRecord with sensible defaults."""
    if sha256_hash is None:
        sha256_hash = uuid.uuid4().hex + uuid.uuid4().hex[:32]  # 64 hex chars
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        agent_id=agent_id,
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=file_size,
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


@pytest.mark.asyncio
async def test_companion_does_not_link_to_other_agents_media_at_same_path(session: AsyncSession) -> None:
    """A companion must link only to media on ITS OWN agent (phaze-vpig).

    original_path is only unique per agent (uq_files_agent_id_original_path), so two
    fileserver agents can hold files at the identical directory path. A companion on
    agent A must not link to agent B's media just because the paths collide."""
    session.add(Agent(id="test-fileserver-b", name="test-fileserver-b", kind="fileserver", scan_roots=[]))
    await session.flush()

    media_a = _make_file("/data/music/coachella24/set.mp3", "mp3")
    comp_a = _make_file("/data/music/coachella24/set.cue", "cue")
    media_b = _make_file("/data/music/coachella24/set.mp3", "mp3", agent_id="test-fileserver-b")
    session.add_all([media_a, comp_a, media_b])
    await session.flush()

    count = await associate_companions(session)

    assert count == 1
    result = await session.execute(select(FileCompanion))
    links = result.scalars().all()
    assert {(link.companion_id, link.media_id) for link in links} == {(comp_a.id, media_a.id)}


@pytest.mark.asyncio
async def test_companions_on_two_agents_each_link_to_own_agents_media(session: AsyncSession) -> None:
    """Companions in the same-named directory on two agents group per agent and each
    link only to their own agent's media (phaze-vpig: the grouping side of the fix)."""
    session.add(Agent(id="test-fileserver-b", name="test-fileserver-b", kind="fileserver", scan_roots=[]))
    await session.flush()

    media_a = _make_file("/data/music/show1/set.mp3", "mp3")
    comp_a = _make_file("/data/music/show1/cover.jpg", "jpg")
    media_b = _make_file("/data/music/show1/set.mp3", "mp3", agent_id="test-fileserver-b")
    comp_b = _make_file("/data/music/show1/info.nfo", "nfo", agent_id="test-fileserver-b")
    session.add_all([media_a, comp_a, media_b, comp_b])
    await session.flush()

    count = await associate_companions(session)

    assert count == 2
    result = await session.execute(select(FileCompanion))
    links = result.scalars().all()
    assert {(link.companion_id, link.media_id) for link in links} == {
        (comp_a.id, media_a.id),
        (comp_b.id, media_b.id),
    }


@pytest.mark.asyncio
async def test_companions_with_underscore_dir_do_not_link_to_sibling_dashed_dir(session: AsyncSession) -> None:
    """A companion in a directory containing '_' must not link to media in a sibling
    directory whose name matches only because '_' is an unescaped LIKE wildcard
    (e.g. Coachella_2024 vs Coachella-2024 or "Coachella 2024")."""
    media_underscore = _make_file("/music/Coachella_2024/track.mp3", "mp3")
    comp_underscore = _make_file("/music/Coachella_2024/cover.jpg", "jpg")
    media_dashed = _make_file("/music/Coachella-2024/other.mp3", "mp3")
    media_spaced = _make_file("/music/Coachella 2024/another.mp3", "mp3")
    session.add_all([media_underscore, comp_underscore, media_dashed, media_spaced])
    await session.flush()

    count = await associate_companions(session)

    assert count == 1
    result = await session.execute(select(FileCompanion))
    links = result.scalars().all()
    link_pairs = {(link.companion_id, link.media_id) for link in links}
    assert link_pairs == {(comp_underscore.id, media_underscore.id)}


@pytest.mark.asyncio
async def test_companions_with_underscore_dir_do_not_link_across_path_separator(session: AsyncSession) -> None:
    """The unescaped '_' wildcard also matches '/', so a directory like 'Set_1' must
    not link to media in an unrelated subdirectory tree such as 'Set/1'."""
    comp = _make_file("/music/Set_1/notes.txt", "txt")
    media_other_tree = _make_file("/music/Set/1/track.mp3", "mp3")
    session.add_all([comp, media_other_tree])
    await session.flush()

    count = await associate_companions(session)

    assert count == 0
    result = await session.execute(select(FileCompanion))
    assert len(result.scalars().all()) == 0
