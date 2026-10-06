"""Tests for companion association service."""

import uuid

import pytest
from sqlalchemy import insert, select
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
async def test_concurrent_run_that_already_linked_the_pair_does_not_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent under concurrency (phaze-u6ml): the unlinked-companion read is a
    snapshot, so a concurrent POST /associate computes the same (companion, media)
    pairs and commits them first. Simulate that interleaving by injecting the
    conflicting FileCompanion row immediately after the snapshot read; the run must
    skip the already-inserted pair (first-writer-wins) instead of raising
    IntegrityError against uq_file_companions_pair and rolling back the batch."""
    media = _make_file("/music/album/track.mp3", "mp3")
    comp = _make_file("/music/album/cover.jpg", "jpg")
    session.add_all([media, comp])
    await session.flush()

    real_execute = session.execute
    injected = False

    async def execute_with_race(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal injected
        result = await real_execute(stmt, *args, **kwargs)
        if not injected:
            # First statement executed is the unlinked-companion snapshot read:
            # right after it, the "other request" commits the identical pair.
            injected = True
            await real_execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=comp.id, media_id=media.id))
        return result

    monkeypatch.setattr(session, "execute", execute_with_race)

    count = await associate_companions(session)

    # The pair already existed by insert time, so nothing new was inserted --
    # the documented idempotent no-op, not an IntegrityError/HTTP 500.
    assert count == 0
    result = await session.execute(select(FileCompanion))
    links = result.scalars().all()
    assert len(links) == 1
    assert (links[0].companion_id, links[0].media_id) == (comp.id, media.id)


@pytest.mark.asyncio
async def test_partial_overlap_with_concurrent_run_inserts_only_missing_pairs(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a concurrent run already linked SOME of the computed pairs, the
    surviving run inserts only the missing ones and counts only those
    (phaze-u6ml: one conflicting row must not roll back the whole batch)."""
    media1 = _make_file("/music/album/track1.mp3", "mp3")
    media2 = _make_file("/music/album/track2.flac", "flac")
    comp = _make_file("/music/album/cover.jpg", "jpg")
    session.add_all([media1, media2, comp])
    await session.flush()

    real_execute = session.execute
    injected = False

    async def execute_with_race(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal injected
        result = await real_execute(stmt, *args, **kwargs)
        if not injected:
            injected = True
            await real_execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=comp.id, media_id=media1.id))
        return result

    monkeypatch.setattr(session, "execute", execute_with_race)

    count = await associate_companions(session)

    assert count == 1
    result = await session.execute(select(FileCompanion))
    links = result.scalars().all()
    assert {(link.companion_id, link.media_id) for link in links} == {
        (comp.id, media1.id),
        (comp.id, media2.id),
    }


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


# phaze-ehryj: a companion with no media of its own links only to media directly in its PARENT
# directory, on the same agent, whose stem matches the companion's sub-folder name or own stem --
# exactly one level, and own-directory media always wins.


async def _link_pairs(session: AsyncSession) -> set[tuple[uuid.UUID, uuid.UUID]]:
    result = await session.execute(select(FileCompanion))
    return {(link.companion_id, link.media_id) for link in result.scalars().all()}


@pytest.mark.asyncio
async def test_subfolder_companion_links_to_parent_media_by_stem_or_folder_name(session: AsyncSession) -> None:
    """A release's info/ companion links by its stem; a release-named folder's companion links by the folder."""
    media = _make_file("/music/set/Live Set 2019.mp3", "mp3")
    by_stem = _make_file("/music/set/info/00-live_set-2019.nfo", "nfo")
    by_folder = _make_file("/music/set/Live.Set.2019/site.txt", "txt")
    unmatched = _make_file("/music/set/playlist 000-049/011.txt", "txt")
    session.add_all([media, by_stem, by_folder, unmatched])
    await session.flush()

    assert await associate_companions(session) == 2
    assert await _link_pairs(session) == {(by_stem.id, media.id), (by_folder.id, media.id)}


@pytest.mark.asyncio
async def test_dump_folder_subfolder_companions_link_only_to_their_named_set(session: AsyncSession) -> None:
    """A flat dump of unrelated sets yields zero links for non-matching names, one for the matching one."""
    # The dump folder's own name looks like a set name too, and must not pair every husk with anything.
    dump = "/music/Artist Live Sets 2019"
    dump_media = [_make_file(f"{dump}/Artist {index} - Live.mp3", "mp3") for index in range(300)]
    husks = [_make_file(f"{dump}/Other {index} - Live/site.nfo", "nfo") for index in range(30)]
    named = _make_file(f"{dump}/Artist 7 - Live/site.nfo", "nfo")
    session.add_all([*dump_media, *husks, named])
    await session.flush()

    assert await associate_companions(session) == 1
    assert await _link_pairs(session) == {(named.id, dump_media[7].id)}


@pytest.mark.asyncio
async def test_name_match_links_every_same_named_parent_media_file(session: AsyncSession) -> None:
    """Two parent media files sharing a stem (one set, two containers) are both linked; others are not."""
    audio = _make_file("/music/set/show.mp3", "mp3")
    video = _make_file("/music/set/show.mkv", "mkv")
    other = _make_file("/music/set/encore.mp3", "mp3")
    cue = _make_file("/music/set/info/show.cue", "cue")
    session.add_all([audio, video, other, cue])
    await session.flush()

    assert await associate_companions(session) == 2
    assert await _link_pairs(session) == {(cue.id, audio.id), (cue.id, video.id)}


@pytest.mark.asyncio
async def test_own_directory_media_wins_over_parent_media(session: AsyncSession) -> None:
    """A sub-folder holding its own media never also links to its parent's, even by name."""
    parent_media = _make_file("/music/set/bonus.mp3", "mp3")
    own_media = _make_file("/music/set/bonus/other.mp3", "mp3")
    cue = _make_file("/music/set/bonus/bonus.cue", "cue")
    session.add_all([parent_media, own_media, cue])
    await session.flush()

    assert await associate_companions(session) == 1
    assert await _link_pairs(session) == {(cue.id, own_media.id)}


@pytest.mark.asyncio
async def test_parent_fallback_is_one_level_and_never_downward(session: AsyncSession) -> None:
    """A grandparent's media is never consulted, and a companion ABOVE media stays unlinked."""
    grand_media = _make_file("/music/set/track.mp3", "mp3")
    too_deep = _make_file("/music/set/info/deep/track.cue", "cue")
    above = _make_file("/music/upper/set.txt", "txt")
    below_media = _make_file("/music/upper/media/set.mp4", "mp4")
    session.add_all([grand_media, too_deep, above, below_media])
    await session.flush()

    assert await associate_companions(session) == 0
    assert await _link_pairs(session) == set()


@pytest.mark.asyncio
async def test_parent_fallback_stays_on_the_companions_agent(session: AsyncSession) -> None:
    """Same-named parent media on ANOTHER agent at the identical path is never a fallback target."""
    session.add(Agent(id="test-fileserver-b", name="test-fileserver-b", kind="fileserver", scan_roots=[]))
    await session.flush()
    other_media = _make_file("/music/set/track.mp3", "mp3", agent_id="test-fileserver-b")
    notes = _make_file("/music/set/info/track.nfo", "nfo")
    session.add_all([other_media, notes])
    await session.flush()

    assert await associate_companions(session) == 0
    assert await _link_pairs(session) == set()


@pytest.mark.asyncio
async def test_companion_at_filesystem_root_has_no_parent_fallback(session: AsyncSession) -> None:
    """A companion directly under '/' has no parent to fall back to, and links nothing."""
    notes = _make_file("/notes.nfo", "nfo")
    session.add(notes)
    await session.flush()

    assert await associate_companions(session) == 0


@pytest.mark.asyncio
async def test_names_with_no_letters_or_digits_never_match(session: AsyncSession) -> None:
    """A media stem and a companion stem that both normalize to the empty key are not a name match."""
    media = _make_file("/music/set/__.mp3", "mp3")
    notes = _make_file("/music/set/info/--.nfo", "nfo")
    session.add_all([media, notes])
    await session.flush()

    assert await associate_companions(session) == 0


@pytest.mark.asyncio
async def test_info_subfolder_companion_links_by_its_release_folders_name(session: AsyncSession) -> None:
    """A release folder named like its one video links its info/ notices, whatever their own names.

    Production shape (operator decision 2026-10-06, "Add parent-folder name"; bead phaze-ehryj): the
    notices are download-site text files and the sub-folder is "info", so only the release folder's
    own name ties them to the video.
    """
    video = _make_file("/video/Band Live - Night 1 (2022)/Band Live_Night 1 (2022) (1).mkv", "mkv")
    notice = _make_file("/video/Band Live - Night 1 (2022)/info/Downloaded from a site.txt", "txt")
    other = _make_file("/video/Band Live - Night 1 (2022)/encore.mkv", "mkv")
    session.add_all([video, notice, other])
    await session.flush()

    assert await associate_companions(session) == 1
    assert await _link_pairs(session) == {(notice.id, video.id)}


@pytest.mark.asyncio
async def test_duplicate_copies_share_a_key_and_both_link(session: AsyncSession) -> None:
    """ "X (1)" and "X (2)" key equal once the copy marker is dropped, so a companion named X links to both."""
    first = _make_file("/music/set/Show (1).mp3", "mp3")
    second = _make_file("/music/set/Show (2).mp3", "mp3")
    other = _make_file("/music/set/Encore.mp3", "mp3")
    cue = _make_file("/music/set/info/Show.cue", "cue")
    session.add_all([first, second, other, cue])
    await session.flush()

    assert await associate_companions(session) == 2
    assert await _link_pairs(session) == {(cue.id, first.id), (cue.id, second.id)}
