"""Control-side storage of companion content features and the known-stamp rule (phaze-osy6j).

Records are produced by the REAL extractor over synthetic files in ``tmp_path`` (``read_companion``
+ ``CompanionFeaturesRecord.from_reading``) -- the bytes, folder listings and fingerprints are the
ones an agent would report. Only the ``files`` rows the scan would have upserted are seeded directly.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.models.agent import Agent
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.schemas.agent_companion_features import CompanionFeaturesRecord
from phaze.services.companion_content import (
    BackfillCounts,
    count_backfill,
    folder_name_key,
    is_known_stamp,
    refresh_known_stamps,
    select_backfill_page,
    store_companion_features,
)
from phaze.services.companion_features import read_companion
from phaze.services.scan_deletion import delete_file_cascade, invalidate_content_state


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT = "test-fileserver"
_STAMP = b"Downloaded from www.example-release-site.test\r\nVisit us for more free sets!\r\nJoin us on the forum.\r\n"
_TRACKLIST = b"\r\nTracklist:\r\n01. Example Artist - First Tune\r\n02. Other Artist - Second Tune\r\n03. Third Artist - Third Tune\r\n"


def _put(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _row(session: AsyncSession, path: Path, *, agent_id: str = _AGENT) -> FileRecord:
    """The files row the scan would have upserted for ``path`` (current bytes, real hash)."""
    payload = path.read_bytes()
    record = FileRecord(
        agent_id=agent_id,
        sha256_hash=hashlib.sha256(payload).hexdigest(),
        original_path=str(path),
        original_filename=path.name,
        current_path=str(path),
        file_type=path.suffix.lstrip(".").lower(),
        file_size=len(payload),
    )
    session.add(record)
    await session.flush()
    return record


def _record(path: Path) -> CompanionFeaturesRecord:
    return CompanionFeaturesRecord.from_reading(str(path), read_companion(str(path)))


async def _features(session: AsyncSession, file_id: uuid.UUID) -> CompanionContentFeatures:
    """The stored row, re-read (the service writes with Core statements, behind the identity map)."""
    statement = select(CompanionContentFeatures).where(CompanionContentFeatures.file_id == file_id).execution_options(populate_existing=True)
    return (await session.execute(statement)).scalar_one()


async def _release(session: AsyncSession, root: Path, name: str, media: str, companion: bytes, filename: str = "site.nfo") -> FileRecord:
    """A release folder holding one media file and one companion; returns the companion's row."""
    _put(root / name / media, b"audio")
    return await _row(session, _put(root / name / filename, companion))


# The rule, pure.


def test_folder_names_compare_alphanumeric_only_and_case_folded() -> None:
    assert folder_name_key("/a/Example_Artist - LIVE (2024)") == folder_name_key("/b/example artist live 2024") == "exampleartistlive2024"


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        # Three differently named folders beside three different recordings: a stamp.
        ([("/r/one", {"a.mp3"}), ("/r/two", {"b.mp3"}), ("/r/three", {"c.mp3"})], True),
        # A duplicated release: the same recording beside every copy, so the media sets intersect.
        ([("/r/one", {"a.mp3", "x.mp3"}), ("/r/two", {"a.mp3", "y.mp3"}), ("/r/three", {"a.mp3", "z.mp3"})], False),
        # The same release downloaded three times keeps its folder name: never three distinct names.
        ([("/x/rel", {"a.mp3"}), ("/y/rel", {"b.mp3"}), ("/z/REL", {"c.mp3"})], False),
        # No media anywhere: three differently named folders suffice.
        ([("/r/one", set()), ("/r/two", set()), ("/r/three", set())], True),
        # Two folders are never enough.
        ([("/r/one", {"a.mp3"}), ("/r/two", {"b.mp3"})], False),
        # Some media, but fewer than three distinct sets: not decidable as a stamp.
        ([("/r/one", {"a.mp3"}), ("/r/two", set()), ("/r/three", set())], False),
    ],
)
def test_is_known_stamp(members: list[tuple[str, set[str]]], expected: bool) -> None:
    assert is_known_stamp(members) is expected


# Storage and the stamp verdict over stored rows.


async def test_a_stamp_beside_three_different_recordings_becomes_known_stamp_on_every_copy(session: AsyncSession, tmp_path: Path) -> None:
    copies = [await _release(session, tmp_path, f"release-{index}", f"set-{index}.mp3", _STAMP) for index in range(3)]
    appended = await _release(session, tmp_path, "release-x", "set-x.mp3", _STAMP + _TRACKLIST)

    # Reported in separate chunks, as separate scan chunks would: the verdict lands on the third.
    first = await store_companion_features(session, _AGENT, [_record(tmp_path / "release-0" / "site.nfo")])
    assert (await _features(session, copies[0].id)).junk_class == "site_ad"
    for index in (1, 2):
        await store_companion_features(session, _AGENT, [_record(tmp_path / f"release-{index}" / "site.nfo")])
    await store_companion_features(session, _AGENT, [_record(tmp_path / "release-x" / "site.nfo")])

    assert first.stored == 1
    for index, copy in enumerate(copies):
        row = await _features(session, copy.id)
        assert row.junk_class == "known_stamp"
        assert row.content_junk_class == "site_ad"
        assert row.fingerprint == copy.sha256_hash
        assert (row.folder_media, row.folder_media_count) == ([f"set-{index}.mp3"], 1)
    kept = await _features(session, appended.id)
    assert kept.junk_class is None  # the stamp with a real tracklist appended is not junk
    assert kept.is_tracklist is True


async def test_unchanged_re_report_keeps_the_verdict_and_a_replaced_copy_un_stamps_the_rest(session: AsyncSession, tmp_path: Path) -> None:
    copies = [await _release(session, tmp_path, f"release-{index}", f"set-{index}.mp3", _STAMP) for index in range(3)]
    paths = [tmp_path / f"release-{index}" / "site.nfo" for index in range(3)]
    await store_companion_features(session, _AGENT, [_record(path) for path in paths])
    assert {(await _features(session, copy.id)).junk_class for copy in copies} == {"known_stamp"}

    await store_companion_features(session, _AGENT, [_record(paths[0])])
    assert (await _features(session, copies[0].id)).junk_class == "known_stamp"

    # One copy's bytes change: the content now sits in only two folders, so it is no stamp.
    _put(paths[2], _STAMP + _TRACKLIST)
    await store_companion_features(session, _AGENT, [_record(paths[2])])
    assert [(await _features(session, copy.id)).junk_class for copy in copies] == ["site_ad", "site_ad", None]


async def test_duplicated_release_copies_are_not_stamps(session: AsyncSession, tmp_path: Path) -> None:
    nfo = b"Artist ....: Example Artist\r\nGenre .....: Trance\r\nSource ....: FM\r\nvisit www.example-site.test to download more\r\n"
    rows = []
    for parent in ("a", "b", "c"):
        _put(tmp_path / parent / "rel" / "set.mp3", b"audio")
        rows.append(await _row(session, _put(tmp_path / parent / "rel" / "info.nfo", nfo)))
    await store_companion_features(session, _AGENT, [_record(tmp_path / parent / "rel" / "info.nfo") for parent in ("a", "b", "c")])

    assert [(await _features(session, row.id)).junk_class for row in rows] == [None, None, None]


async def test_empty_bytes_outrank_a_stamp_verdict(session: AsyncSession, tmp_path: Path) -> None:
    rows = [await _release(session, tmp_path, f"release-{index}", f"set-{index}.mp3", b"\x00" * 512) for index in range(3)]
    await store_companion_features(session, _AGENT, [_record(tmp_path / f"release-{index}" / "site.nfo") for index in range(3)])

    stamps = await refresh_known_stamps(session, _AGENT, {rows[0].sha256_hash})
    assert stamps == {rows[0].sha256_hash}  # the content IS spread like a stamp...
    assert {(await _features(session, row.id)).junk_class for row in rows} == {"all_nul"}  # ...but empty wins


async def test_records_land_only_on_the_agents_own_companion_rows(session: AsyncSession, tmp_path: Path) -> None:
    session.add(Agent(id="other-fileserver", name="other-fileserver", token_hash=uuid.uuid4().hex * 2, scan_roots=[str(tmp_path)]))
    own = await _row(session, _put(tmp_path / "rel" / "own.cue", b'FILE "set.mp3" MP3\r\n'))
    media = await _row(session, _put(tmp_path / "rel" / "set.mp3", b"audio"))
    other_agent = await _row(session, _put(tmp_path / "rel" / "theirs.cue", b'FILE "set.mp3" MP3\r\n'), agent_id="other-fileserver")
    stray = tmp_path / "rel" / "never-ingested.cue"
    _put(stray, b'FILE "set.mp3" MP3\r\n')

    outcome = await store_companion_features(
        session,
        _AGENT,
        [
            _record(tmp_path / "rel" / "own.cue"),
            _record(tmp_path / "rel" / "theirs.cue"),
            _record(stray),
            # A record naming a MEDIA row never lands, whatever it carries.
            CompanionFeaturesRecord.from_reading(str(tmp_path / "rel" / "set.mp3"), read_companion(str(tmp_path / "rel" / "own.cue"))),
        ],
    )

    assert (outcome.stored, outcome.unknown) == (1, 3)
    stored = (await session.execute(select(CompanionContentFeatures.file_id))).scalars().all()
    assert stored == [own.id]
    assert media.id not in stored
    assert other_agent.id not in stored
    row = await _features(session, own.id)
    assert row.agent_id == _AGENT
    assert row.media_references == [{"name": "set.mp3", "source": "cue_file"}]
    assert row.folder_media == ["set.mp3"]


async def test_backfill_counts_and_pages_select_missing_stale_and_old_extractor_rows(session: AsyncSession, tmp_path: Path) -> None:
    current = await _row(session, _put(tmp_path / "a" / "current.txt", b"01. A - B\n02. C - D\n03. E - F\n"))
    stale = await _row(session, _put(tmp_path / "a" / "stale.txt", b"first version of the notes, long enough\n"))
    old_extractor = await _row(session, _put(tmp_path / "a" / "old.txt", b"notes long enough to not be empty here\n"))
    missing = await _row(session, _put(tmp_path / "b" / "missing.nfo", b"notes long enough to not be empty here\n"))
    await _row(session, _put(tmp_path / "b" / "media.mp3", b"audio"))  # never a backfill target
    await store_companion_features(session, _AGENT, [_record(tmp_path / "a" / name) for name in ("current.txt", "stale.txt", "old.txt")])
    stale.sha256_hash = "0" * 64  # the file changed after its features were read
    (await _features(session, old_extractor.id)).extractor_version = 0
    await session.flush()

    counts = await count_backfill(session)

    assert counts == [BackfillCounts(agent_id=_AGENT, companions=4, current=1, missing=1, stale_content=1, stale_extractor=1)]
    assert counts[0].pending == 3
    pages = []
    after = None
    while page := await select_backfill_page(session, _AGENT, after=after, limit=2):
        pages.append(page)
        after = (page[-1][1], page[-1][0])
    assert [len(page) for page in pages] == [2, 1]
    assert {file_id for page in pages for file_id, _path in page} == {stale.id, old_extractor.id, missing.id}
    assert current.id not in {file_id for page in pages for file_id, _path in page}


async def test_a_content_change_drops_the_features_and_a_deletion_cascades_them(session: AsyncSession, tmp_path: Path) -> None:
    changed = await _row(session, _put(tmp_path / "a" / "changed.cue", b'FILE "a.mp3" MP3\r\n'))
    deleted = await _row(session, _put(tmp_path / "a" / "deleted.cue", b'FILE "b.mp3" MP3\r\n'))
    neighbour = await _row(session, _put(tmp_path / "a" / "neighbour.cue", b'FILE "c.mp3" MP3\r\n'))
    await store_companion_features(session, _AGENT, [_record(tmp_path / "a" / name) for name in ("changed.cue", "deleted.cue", "neighbour.cue")])

    assert (await invalidate_content_state(session, changed.id))["companion_content_features"] == 1
    assert (await delete_file_cascade(session, deleted.id))["companion_content_features"] == 1

    remaining = (await session.execute(select(CompanionContentFeatures.file_id))).scalars().all()
    assert remaining == [neighbour.id]


async def test_a_chunk_naming_no_row_of_the_agent_stores_nothing(session: AsyncSession, tmp_path: Path) -> None:
    stray = _put(tmp_path / "rel" / "never-ingested.nfo", b"notes long enough to not be empty here\n")

    outcome = await store_companion_features(session, _AGENT, [_record(stray)])

    assert (outcome.stored, outcome.unknown) == (0, 1)
    assert (await session.execute(select(CompanionContentFeatures.file_id))).all() == []
