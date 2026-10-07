"""Tests for the companion association service: the linking chain run over stored features (phaze-rmhfr).

Rows and their ``companion_content_features`` are seeded directly, with the fingerprint set to the
row's ``sha256_hash`` (current features). The real extractor feeding the real association is covered
end to end by ``tests/discovery/test_companion_ingestion_producers.py``; the chain's own rules, one
by one, by ``tests/discovery/services/test_companion_linking.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import func, insert, select

from phaze.models.agent import Agent
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
import phaze.services.companion as companion_module
from phaze.services.companion import associate_companions


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT = "test-fileserver"


def _make_file(original_path: str, file_type: str, sha256_hash: str | None = None, agent_id: str = _AGENT) -> FileRecord:
    """A files row with sensible defaults; ``file_type`` is the lowercased extension."""
    if sha256_hash is None:
        sha256_hash = uuid.uuid4().hex + uuid.uuid4().hex
    return FileRecord(
        agent_id=agent_id,
        id=uuid.uuid4(),
        sha256_hash=sha256_hash,
        original_path=original_path,
        original_filename=original_path.rsplit("/", 1)[-1],
        current_path=original_path,
        file_type=file_type,
        file_size=1000,
    )


def _features(
    record: FileRecord,
    *references: str,
    junk_class: str | None = None,
    is_tracklist: bool = False,
    fingerprint: str | None = None,
) -> CompanionContentFeatures:
    """Current stored features for ``record``: its references (CUE ``FILE`` source), junk class and tracklist flag."""
    return CompanionContentFeatures(
        file_id=record.id,
        agent_id=record.agent_id,
        fingerprint=fingerprint or record.sha256_hash,
        encoding="ascii",
        byte_size=100,
        media_references=[{"name": name, "source": "cue_file"} for name in references],
        reference_count=len(references),
        is_tracklist=is_tracklist,
        content_junk_class=junk_class if junk_class != "known_stamp" else None,
        junk_class=junk_class,
        extractor_version=1,
    )


async def _seed(session: AsyncSession, *rows: FileRecord | CompanionContentFeatures) -> None:
    """Add files rows first, then features (their FK), and flush."""
    session.add_all([row for row in rows if isinstance(row, FileRecord)])
    await session.flush()
    session.add_all([row for row in rows if isinstance(row, CompanionContentFeatures)])
    await session.flush()


def _companion(path: str, *references: str, **kwargs: object) -> tuple[FileRecord, CompanionContentFeatures]:
    record = _make_file(path, path.rsplit(".", 1)[-1].lower())
    return record, _features(record, *references, **kwargs)  # type: ignore[arg-type]


async def _link_pairs(session: AsyncSession) -> set[tuple[uuid.UUID, uuid.UUID]]:
    result = await session.execute(select(FileCompanion))
    return {(link.companion_id, link.media_id) for link in result.scalars().all()}


@pytest.mark.asyncio
async def test_companion_links_to_every_media_file_in_its_own_folder(session: AsyncSession) -> None:
    """Step 4, the shipped own-folder rule: a release note with no filename inside links to every part beside it."""
    media1 = _make_file("/music/album/track1.mp3", "mp3")
    media2 = _make_file("/music/album/track2.flac", "flac")
    nfo, nfo_features = _companion("/music/album/release.nfo")
    await _seed(session, media1, media2, nfo, nfo_features)

    outcome = await associate_companions(session)

    assert outcome.links_created == 2
    assert outcome.decided == {"folder": 1}
    assert await _link_pairs(session) == {(nfo.id, media1.id), (nfo.id, media2.id)}


@pytest.mark.asyncio
async def test_companion_no_media_anywhere_links_nothing(session: AsyncSession) -> None:
    notes, notes_features = _companion("/docs/readme.txt")
    await _seed(session, notes, notes_features)

    outcome = await associate_companions(session)

    assert outcome.links_created == 0
    assert outcome.decided == {"unlinked": 1}
    assert await _link_pairs(session) == set()


@pytest.mark.asyncio
async def test_idempotent_association(session: AsyncSession) -> None:
    """A second run re-derives the same links: it decides again, and keeps every link without a delete or an insert."""
    media = _make_file("/music/album/track.mp3", "mp3")
    cue, cue_features = _companion("/music/album/track.cue", "track.mp3")
    await _seed(session, media, cue, cue_features)

    first = await associate_companions(session)
    second = await associate_companions(session)

    assert (first.links_created, first.links_removed, first.links_kept) == (1, 0, 0)
    assert (second.links_created, second.links_removed, second.links_kept) == (0, 0, 1)
    assert second.decided == {"reference": 1}
    assert await _link_pairs(session) == {(cue.id, media.id)}


@pytest.mark.asyncio
async def test_existing_links_are_replaced_by_exactly_what_the_chain_derives(session: AsyncSession) -> None:
    """Operator decision 2026-10-07, "Re-derive every link (Recommended)" (bead phaze-rmhfr comment).

    An over-link (a tracklist linked to every mix in its folder by the old own-folder rule) and a
    retired parent-folder link are both removed; the one link the chain still derives is kept, not
    rewritten; the missing one is inserted.
    """
    mix1 = _make_file("/music/mixes/Mix 01.mp3", "mp3")
    mix2 = _make_file("/music/mixes/Mix 02.mp3", "mp3")
    mix3 = _make_file("/music/mixes/Mix 03.mp3", "mp3")
    tracklist, tracklist_features = _companion("/music/mixes/Mix 02.txt", is_tracklist=True)
    parent_media = _make_file("/music/release/Live Set 2019.mp3", "mp3")
    sub_notes, sub_notes_features = _companion("/music/release/info/00-live_set-2019.nfo")
    cue, cue_features = _companion("/music/mixes/mix-03.cue", "Mix 03.mp3", "Mix 01.mp3")
    await _seed(session, mix1, mix2, mix3, tracklist, tracklist_features, parent_media, sub_notes, sub_notes_features, cue, cue_features)
    old = [(tracklist, mix1), (tracklist, mix2), (tracklist, mix3), (sub_notes, parent_media), (cue, mix3)]
    for companion, media in old:
        await session.execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=companion.id, media_id=media.id))
    kept_link_id = (await session.execute(select(FileCompanion.id).where(FileCompanion.companion_id == cue.id))).scalar_one()

    outcome = await associate_companions(session)

    assert (outcome.links_created, outcome.links_removed, outcome.links_kept) == (1, 3, 2)
    assert await _link_pairs(session) == {(tracklist.id, mix2.id), (cue.id, mix3.id), (cue.id, mix1.id)}
    assert kept_link_id in set((await session.execute(select(FileCompanion.id))).scalars())

    again = await associate_companions(session)
    assert (again.links_created, again.links_removed, again.links_kept) == (0, 0, 3)


@pytest.mark.asyncio
async def test_a_companion_without_current_features_keeps_its_links_untouched(session: AsyncSession) -> None:
    """Re-derivation needs features: a companion still awaiting them keeps every link it has, even one the chain would drop."""
    media = _make_file("/music/release/Live Set 2019.mp3", "mp3")
    never_read = _make_file("/music/release/info/notes.nfo", "nfo")
    stale = _make_file("/music/release/info/old.cue", "cue")
    decided, decided_features = _companion("/music/release/info/x.nfo")
    await _seed(session, media, never_read, stale, _features(stale, fingerprint="d" * 64), decided, decided_features)
    for companion in (never_read, stale, decided):
        await session.execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=companion.id, media_id=media.id))

    outcome = await associate_companions(session)

    assert (outcome.links_removed, outcome.awaiting_features) == (1, 2)
    assert await _link_pairs(session) == {(never_read.id, media.id), (stale.id, media.id)}


@pytest.mark.asyncio
async def test_companions_link_only_to_their_own_folders_media(session: AsyncSession) -> None:
    media_a = _make_file("/music/albumA/track.mp3", "mp3")
    nfo_a, nfo_a_features = _companion("/music/albumA/info.nfo")
    media_b = _make_file("/music/albumB/song.ogg", "ogg")
    nfo_b, nfo_b_features = _companion("/music/albumB/info.nfo")
    await _seed(session, media_a, nfo_a, nfo_a_features, media_b, nfo_b, nfo_b_features)

    assert (await associate_companions(session)).links_created == 2
    assert await _link_pairs(session) == {(nfo_a.id, media_a.id), (nfo_b.id, media_b.id)}


@pytest.mark.asyncio
async def test_concurrent_run_that_already_linked_the_pair_does_not_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent under concurrency (phaze-u6ml): the existing-link read is a snapshot, so a concurrent
    POST /associate computes the same pairs and commits them first. Simulate that by inserting the
    conflicting link right after that read; the run must skip it (first-writer-wins) instead of
    raising IntegrityError against uq_file_companions_pair and rolling back the page."""
    media = _make_file("/music/album/track.mp3", "mp3")
    nfo, nfo_features = _companion("/music/album/info.nfo")
    await _seed(session, media, nfo, nfo_features)

    real_read = companion_module._existing_links
    injected = False

    async def read_then_race(*args: object, **kwargs: object) -> object:
        nonlocal injected
        existing = await real_read(*args, **kwargs)  # type: ignore[arg-type]
        if not injected:
            injected = True
            await session.execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=nfo.id, media_id=media.id))
        return existing

    monkeypatch.setattr(companion_module, "_existing_links", read_then_race)

    outcome = await associate_companions(session)

    assert injected
    assert outcome.links_created == 0
    assert await _link_pairs(session) == {(nfo.id, media.id)}


@pytest.mark.asyncio
async def test_partial_overlap_with_concurrent_run_inserts_only_missing_pairs(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """One conflicting row must not roll back the page: only the missing pair is inserted and counted (phaze-u6ml)."""
    media1 = _make_file("/music/album/track1.mp3", "mp3")
    media2 = _make_file("/music/album/track2.flac", "flac")
    nfo, nfo_features = _companion("/music/album/info.nfo")
    await _seed(session, media1, media2, nfo, nfo_features)

    real_read = companion_module._existing_links
    injected = False

    async def read_then_race(*args: object, **kwargs: object) -> object:
        nonlocal injected
        existing = await real_read(*args, **kwargs)  # type: ignore[arg-type]
        if not injected:
            injected = True
            await session.execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=nfo.id, media_id=media1.id))
        return existing

    monkeypatch.setattr(companion_module, "_existing_links", read_then_race)

    outcome = await associate_companions(session)

    assert outcome.links_created == 1
    assert await _link_pairs(session) == {(nfo.id, media1.id), (nfo.id, media2.id)}


@pytest.mark.asyncio
async def test_companion_does_not_link_to_other_agents_media_at_same_path(session: AsyncSession) -> None:
    """A companion links only to media on ITS OWN agent (phaze-vpig), by folder and by reference alike."""
    session.add(Agent(id="test-fileserver-b", name="test-fileserver-b", kind="fileserver", scan_roots=[]))
    await session.flush()
    media_a = _make_file("/data/music/coachella24/set.mp3", "mp3")
    cue_a, cue_a_features = _companion("/data/music/coachella24/set.cue", "set.mp3")
    media_b = _make_file("/data/music/coachella24/set.mp3", "mp3", agent_id="test-fileserver-b")
    only_on_b = _make_file("/data/music/elsewhere/unique-on-b.mp3", "mp3", agent_id="test-fileserver-b")
    cue_ref, cue_ref_features = _companion("/data/music/notes/pointer.cue", "unique-on-b.mp3")
    await _seed(session, media_a, cue_a, cue_a_features, media_b, only_on_b, cue_ref, cue_ref_features)

    outcome = await associate_companions(session)

    assert outcome.links_created == 1
    assert await _link_pairs(session) == {(cue_a.id, media_a.id)}


@pytest.mark.asyncio
async def test_companions_on_two_agents_each_link_to_own_agents_media(session: AsyncSession) -> None:
    """Same-named folders on two agents: each companion links to its own agent's media (phaze-vpig)."""
    session.add(Agent(id="test-fileserver-b", name="test-fileserver-b", kind="fileserver", scan_roots=[]))
    await session.flush()
    media_a = _make_file("/data/music/show1/set.mp3", "mp3")
    nfo_a, nfo_a_features = _companion("/data/music/show1/info.nfo")
    media_b = _make_file("/data/music/show1/set.mp3", "mp3", agent_id="test-fileserver-b")
    nfo_b = _make_file("/data/music/show1/info.nfo", "nfo", agent_id="test-fileserver-b")
    await _seed(session, media_a, nfo_a, nfo_a_features, media_b, nfo_b, _features(nfo_b))

    assert (await associate_companions(session)).links_created == 2
    assert await _link_pairs(session) == {(nfo_a.id, media_a.id), (nfo_b.id, media_b.id)}


@pytest.mark.asyncio
async def test_companions_with_underscore_dir_do_not_link_to_sibling_dashed_dir(session: AsyncSession) -> None:
    """'_' in a folder name is a literal (the pre-phaze-rmhfr LIKE escape), never a wildcard."""
    media_underscore = _make_file("/music/Coachella_2024/track.mp3", "mp3")
    nfo, nfo_features = _companion("/music/Coachella_2024/info.nfo")
    media_dashed = _make_file("/music/Coachella-2024/other.mp3", "mp3")
    media_spaced = _make_file("/music/Coachella 2024/another.mp3", "mp3")
    await _seed(session, media_underscore, nfo, nfo_features, media_dashed, media_spaced)

    assert (await associate_companions(session)).links_created == 1
    assert await _link_pairs(session) == {(nfo.id, media_underscore.id)}


@pytest.mark.asyncio
async def test_companions_with_underscore_dir_do_not_link_across_path_separator(session: AsyncSession) -> None:
    notes, notes_features = _companion("/music/Set_1/notes.txt")
    media_other_tree = _make_file("/music/Set/1/track.mp3", "mp3")
    await _seed(session, notes, notes_features, media_other_tree)

    assert (await associate_companions(session)).links_created == 0


# --- phaze-rmhfr: the chain's content steps through the real association path ---------------------


@pytest.mark.asyncio
async def test_a_reference_links_media_anywhere_on_the_agent_before_any_location_rule(session: AsyncSession) -> None:
    """Step 2 first: a media-less folder's CUE links the file it names in another folder, and a reference beats own-folder media."""
    elsewhere = _make_file("/archive/releases/Set A/set-a.mp3", "mp3")
    orphan_cue, orphan_cue_features = _companion("/archive/copies/Set A copy/set-a.cue", "set-a.mp3")
    part1 = _make_file("/archive/releases/Set B/set-b-part1.mp3", "mp3")
    part2 = _make_file("/archive/releases/Set B/set-b-part2.mp3", "mp3")
    part_cue, part_cue_features = _companion("/archive/releases/Set B/set-b.cue", "set-b-part2.mp3")
    await _seed(session, elsewhere, orphan_cue, orphan_cue_features, part1, part2, part_cue, part_cue_features)

    outcome = await associate_companions(session)

    assert outcome.decided == {"reference": 2}
    assert await _link_pairs(session) == {(orphan_cue.id, elsewhere.id), (part_cue.id, part2.id)}


@pytest.mark.asyncio
async def test_release_folder_twin_links_a_media_less_copy_of_a_release_folder(session: AsyncSession) -> None:
    """Step 5: the companion's folder has no media, and exactly one other folder of the same release name does."""
    media = _make_file("/archive/a/Artist-Live_At_Venue-2019/01-artist-live.mp3", "mp3")
    nfo, nfo_features = _companion("/archive/b/Artist-Live_At_Venue-2019/00-release.nfo")
    await _seed(session, media, nfo, nfo_features)

    outcome = await associate_companions(session)

    assert outcome.decided == {"twin": 1}
    assert await _link_pairs(session) == {(nfo.id, media.id)}


@pytest.mark.asyncio
async def test_junk_companions_are_never_linked_and_their_old_links_are_removed(session: AsyncSession) -> None:
    """The veto: a junk companion beside media gets no link, and a link an older rule wrote is deleted."""
    media = _make_file("/music/set/live.mp3", "mp3")
    stamp, stamp_features = _companion("/music/set/site.nfo", junk_class="known_stamp")
    empty, empty_features = _companion("/music/set/empty.txt", junk_class="empty")
    real, real_features = _companion("/music/set/live.cue", "live.mp3")
    await _seed(session, media, stamp, stamp_features, empty, empty_features, real, real_features)
    await session.execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=stamp.id, media_id=media.id))

    outcome = await associate_companions(session)

    assert outcome.links_removed == 1
    assert outcome.links_created == 1
    assert outcome.decided == {"junk": 2, "reference": 1}
    assert await _link_pairs(session) == {(real.id, media.id)}


@pytest.mark.asyncio
async def test_a_duplicate_orphan_is_never_linked_whatever_its_ingest_order(session: AsyncSession) -> None:
    """Decision 1: a byte-identical copy in a media-less folder goes to the junk review, not to a second link.

    The copy sorts FIRST by id and its own reference resolves, so a single id-ordered walk would link
    it before its original; deciding companions beside media first is what makes the veto see the
    original's link.
    """
    shared = "a" * 64
    media = _make_file("/archive/Set C/set-c.mp3", "mp3")
    original = _make_file("/archive/Set C/set-c.cue", "cue", sha256_hash=shared)
    copy = _make_file("/archive/leftovers/Set C/set-c.cue", "cue", sha256_hash=shared)
    copy.id, original.id = sorted([uuid.uuid4(), uuid.uuid4()])
    await _seed(session, media, original, copy, _features(original, "set-c.mp3"), _features(copy, "set-c.mp3"))

    outcome = await associate_companions(session, batch_size=1)

    assert outcome.decided == {"reference": 1, "duplicate": 1}
    assert await _link_pairs(session) == {(original.id, media.id)}


@pytest.mark.asyncio
async def test_two_media_less_copies_on_one_page_link_once(session: AsyncSession) -> None:
    """Two identical copies, neither beside media, decided on the same page: the first links, the second is a duplicate."""
    shared = "b" * 64
    media = _make_file("/archive/releases/Set D/set-d.mp3", "mp3")
    first = _make_file("/archive/copy1/notes.cue", "cue", sha256_hash=shared)
    second = _make_file("/archive/copy2/notes.cue", "cue", sha256_hash=shared)
    first.id, second.id = sorted([uuid.uuid4(), uuid.uuid4()])
    await _seed(session, media, first, second, _features(first, "set-d.mp3"), _features(second, "set-d.mp3"))

    outcome = await associate_companions(session)

    assert outcome.decided == {"reference": 1, "duplicate": 1}
    assert await _link_pairs(session) == {(first.id, media.id)}


@pytest.mark.asyncio
async def test_a_collection_folders_tracklist_links_only_by_reference_or_stem(session: AsyncSession) -> None:
    """Operator decision 2 (2026-10-07, epic phaze-4x319): a tracklist beside many dated episodes needs a reference or stem match."""
    folder = "/radio/Show 2011 03"
    episodes = [_make_file(f"{folder}/2011 03 {day:02d} Show #{number} Part 1.mp3", "mp3") for day, number in ((5, 279), (12, 280), (19, 281))]
    unnamed, unnamed_features = _companion(f"{folder}/Show #279.txt", is_tracklist=True)
    by_stem, by_stem_features = _companion(f"{folder}/2011 03 12 Show #280 Part 1.txt", is_tracklist=True)
    by_reference, by_reference_features = _companion(f"{folder}/notes.txt", episodes[2].original_filename, is_tracklist=True)
    release_note, release_note_features = _companion(f"{folder}/series.nfo")
    await _seed(session, *episodes, unnamed, unnamed_features, by_stem, by_stem_features, by_reference, by_reference_features)
    await _seed(session, release_note, release_note_features)

    outcome = await associate_companions(session)

    assert outcome.decided == {"unlinked": 1, "stem": 1, "reference": 1, "folder": 1}
    pairs = await _link_pairs(session)
    assert {pair for pair in pairs if pair[0] != release_note.id} == {(by_stem.id, episodes[1].id), (by_reference.id, episodes[2].id)}


@pytest.mark.asyncio
async def test_companions_without_current_features_wait_and_are_counted(session: AsyncSession) -> None:
    """No features row, or features of older bytes: never decided by location alone, and reported as awaiting."""
    media = _make_file("/music/album/track.mp3", "mp3")
    never_read = _make_file("/music/album/info.nfo", "nfo")
    stale = _make_file("/music/album/track.cue", "cue")
    await _seed(session, media, never_read, stale, _features(stale, "track.mp3", fingerprint="c" * 64))

    outcome = await associate_companions(session)

    assert outcome.links_created == 0
    assert outcome.awaiting_features == 2
    assert outcome.decided == {}
    assert await _link_pairs(session) == set()


@pytest.mark.asyncio
async def test_a_head_only_junk_verdict_does_not_veto_but_a_known_stamp_does(session: AsyncSession) -> None:
    """A per-file class from a TRUNCATED read judged only the head; a known stamp is a whole-file fingerprint verdict."""
    media = _make_file("/music/set/live.mp3", "mp3")
    big_ad = _make_file("/music/set/huge-notes.txt", "txt")
    big_stamp = _make_file("/music/set/huge-stamp.txt", "txt")
    small_ad, small_ad_features = _companion("/music/set/ad.txt", junk_class="site_ad")
    big_ad_features = _features(big_ad, junk_class="site_ad")
    big_ad_features.truncated = True
    big_stamp_features = _features(big_stamp, junk_class="known_stamp")
    big_stamp_features.truncated = True
    await _seed(session, media, big_ad, big_stamp, big_ad_features, big_stamp_features, small_ad, small_ad_features)

    outcome = await associate_companions(session)

    assert outcome.decided == {"folder": 1, "junk": 2}
    assert await _link_pairs(session) == {(big_ad.id, media.id)}


@pytest.mark.asyncio
async def test_the_duplicate_rule_is_stable_across_a_full_re_derive_from_any_prior_state(session: AsyncSession) -> None:
    """Linked copies only (operator-confirmed 2026-10-07, relayed by the dispatcher; epic phaze-4x319): the copy that links does not depend on which copy an older run linked.

    The prior state links the HIGHER-id media-less copy and the copy of a beside-media original (as the
    retired rules could). Re-derivation judges duplicates on this run's links alone, so: the original
    links, its media-less copy is a duplicate, the lower-id media-less copy links, the higher-id one is a
    duplicate -- and a second run changes nothing.
    """
    media = _make_file("/archive/Set C/set-c.mp3", "mp3")
    original = _make_file("/archive/Set C/set-c.cue", "cue", sha256_hash="a" * 64)
    orphan_copy = _make_file("/archive/leftovers/Set C/set-c.cue", "cue", sha256_hash="a" * 64)
    other_media = _make_file("/archive/releases/Set D/set-d.mp3", "mp3")
    first = _make_file("/archive/copy1/notes.cue", "cue", sha256_hash="b" * 64)
    second = _make_file("/archive/copy2/notes.cue", "cue", sha256_hash="b" * 64)
    first.id, second.id = sorted([uuid.uuid4(), uuid.uuid4()])
    await _seed(
        session,
        media,
        original,
        orphan_copy,
        other_media,
        first,
        second,
        _features(original, "set-c.mp3"),
        _features(orphan_copy, "set-c.mp3"),
        _features(first, "set-d.mp3"),
        _features(second, "set-d.mp3"),
    )
    for companion, target in ((orphan_copy, media), (second, other_media)):
        await session.execute(insert(FileCompanion).values(id=uuid.uuid4(), companion_id=companion.id, media_id=target.id))

    outcome = await associate_companions(session, batch_size=1)

    assert outcome.decided == {"reference": 2, "duplicate": 2}
    expected = {(original.id, media.id), (first.id, other_media.id)}
    assert await _link_pairs(session) == expected
    again = await associate_companions(session)
    assert (again.links_created, again.links_removed, again.decided) == (0, 0, {"reference": 2, "duplicate": 2})
    assert await _link_pairs(session) == expected


# --- phaze-ehryj's parent-folder name fallback, RETIRED by phaze-rmhfr ------------------------------
# Operator decision 8 (2026-10-07, "Yes, file it (Recommended)"; epic phaze-4x319) retired it: phaze-9aker
# measured it at link precision 0.125-0.345 with 7 of 8 links into the archive's dump folder wrong. These
# are ehryj's own fixtures, kept and inverted: each asserts the parent-folder pairing is gone, and says what
# links instead when a measured chain step legitimately does.


@pytest.mark.asyncio
async def test_retired_ehryj_a_subfolder_companion_no_longer_pairs_with_parent_media_by_name(session: AsyncSession) -> None:
    """ehryj linked both of these to the parent's media by key; with no reference and no close name, neither links now."""
    media = _make_file("/music/release/Live Set 2019.mp3", "mp3")
    by_stem, by_stem_features = _companion("/music/release/info/00-live_set-2019.nfo")
    notice, notice_features = _companion("/video/Band Live - Night 1 (2022)/info/Downloaded from a site.txt")
    video = _make_file("/video/Band Live - Night 1 (2022)/Band Live_Night 1 (2022) (1).mkv", "mkv")
    unmatched, unmatched_features = _companion("/music/release/playlist 000-049/011.txt")
    await _seed(session, media, by_stem, by_stem_features, notice, notice_features, video, unmatched, unmatched_features)

    outcome = await associate_companions(session)

    assert outcome.links_created == 0
    assert outcome.decided == {"unlinked": 3}


@pytest.mark.asyncio
async def test_retired_ehryj_dump_folder_subfolders_never_pair_by_proximity(session: AsyncSession) -> None:
    """A flat dump of unrelated sets: the husks link nothing; the one sub-folder named exactly like a set links by close name.

    Under ehryj the named one linked by its folder's key against the dump; now it is step 6 (whole-agent
    close name at 0.9 with a 0.05 margin) that finds it -- the spike's (dw), not a parent-folder rule.
    """
    dump = "/music/Artist Live Sets 2019"
    dump_media = [_make_file(f"{dump}/Artist {index} - Live.mp3", "mp3") for index in range(300)]
    husks = [_companion(f"{dump}/Unrelated Band {chr(65 + index % 26)}{index} Show/site.nfo") for index in range(30)]
    named, named_features = _companion(f"{dump}/Artist 7 - Live/site.nfo")
    await _seed(session, *dump_media, *[part for husk in husks for part in husk], named, named_features)

    outcome = await associate_companions(session)

    assert outcome.decided == {"unlinked": 30, "close_name": 1}
    assert await _link_pairs(session) == {(named.id, dump_media[7].id)}


@pytest.mark.asyncio
async def test_retired_ehryj_own_folder_media_still_wins_over_parent_media(session: AsyncSession) -> None:
    """Unchanged by the retirement: a sub-folder holding its own media links there, never to the parent's."""
    parent_media = _make_file("/music/set/bonus.mp3", "mp3")
    own_media = _make_file("/music/set/bonus/other.mp3", "mp3")
    nfo, nfo_features = _companion("/music/set/bonus/bonus.nfo")
    await _seed(session, parent_media, own_media, nfo, nfo_features)

    assert (await associate_companions(session)).links_created == 1
    assert await _link_pairs(session) == {(nfo.id, own_media.id)}


@pytest.mark.asyncio
async def test_retired_ehryj_parent_media_on_another_agent_is_still_never_a_target(session: AsyncSession) -> None:
    session.add(Agent(id="test-fileserver-b", name="test-fileserver-b", kind="fileserver", scan_roots=[]))
    await session.flush()
    other_media = _make_file("/music/set/track.mp3", "mp3", agent_id="test-fileserver-b")
    notes, notes_features = _companion("/music/set/info/track.nfo")
    await _seed(session, other_media, notes, notes_features)

    assert (await associate_companions(session)).links_created == 0


@pytest.mark.asyncio
async def test_a_companion_at_the_filesystem_root_links_nothing(session: AsyncSession) -> None:
    notes, notes_features = _companion("/notes.nfo")
    await _seed(session, notes, notes_features)

    assert (await associate_companions(session)).links_created == 0


@pytest.mark.asyncio
async def test_no_agent_with_features_means_no_reads_past_the_agent_list(session: AsyncSession) -> None:
    """An archive with no stored features (production before the backfill) decides nothing and counts what waits."""
    media = _make_file("/music/album/track.mp3", "mp3")
    nfo = _make_file("/music/album/info.nfo", "nfo")
    await _seed(session, media, nfo)

    outcome = await associate_companions(session)

    assert (outcome.links_created, outcome.links_removed, outcome.awaiting_features, outcome.decided) == (0, 0, 1, {})
    assert (await session.execute(select(func.count()).select_from(FileCompanion))).scalar_one() == 0


@pytest.mark.asyncio
async def test_the_media_index_reads_every_media_row_of_the_agent_across_keyset_pages(session: AsyncSession) -> None:
    """The index is read in keyset pages on (agent_id, original_path); a page boundary loses and repeats nothing."""
    session.add(Agent(id="test-fileserver-b", name="test-fileserver-b", kind="fileserver", scan_roots=[]))
    await session.flush()
    media = [_make_file(f"/music/set {index}/track.mp3", "mp3") for index in range(5)]
    elsewhere = _make_file("/music/set 0/track.mp3", "mp3", agent_id="test-fileserver-b")
    notes, notes_features = _companion("/music/set 0/notes.nfo")
    await _seed(session, *media, elsewhere, notes, notes_features)

    index = await companion_module._media_index(session, _AGENT, page_size=2)

    assert len(index) == 5
    assert {media_id for folder in {f"/music/set {i}" for i in range(5)} for _name, media_id in index.in_folder(folder)} == {row.id for row in media}
