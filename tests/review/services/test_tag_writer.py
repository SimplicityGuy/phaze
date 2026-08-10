"""Tests for the tag writer service."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from mutagen.id3 import ID3, TCON, TDRC, TRCK
from mutagen.mp3 import MP3
import pytest
from sqlalchemy import select

from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.tag_write_disk import _extract_before_tags as _extract_before_tags_disk, _mp4_track_tuple, write_and_verify_sync
from phaze.services.tag_writer import (
    TagWriteAlreadyQueuedError,
    _extract_before_tags,
    _write_id3,
    _write_mp4,
    _write_vorbis,
    enqueue_tag_write,
    verify_write,
    write_tags,
)


async def _add_proposal(session: AsyncSession, file_id: uuid.UUID, status: str) -> None:
    """Insert one ``RenameProposal`` with ``status`` for ``file_id`` and commit."""
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename="Renamed Set.mp3",
            proposed_path=None,
            confidence=0.95,
            status=status,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Fixtures: minimal valid audio files
# ---------------------------------------------------------------------------


def _make_mp3(path: Path) -> Path:
    """Create a minimal valid MP3 file with multiple MPEG frames + ID3 tags."""
    # MPEG1 Layer3 128kbps 44100Hz stereo, no padding, no CRC
    header = struct.pack(">I", 0xFFFB9000)
    frame_size = 417  # 144 * 128000 / 44100 = 417 bytes
    frame = header + b"\x00" * (frame_size - 4)
    # Write 10 frames so mutagen can sync properly
    path.write_bytes(frame * 10)
    # Add ID3 tags via mutagen
    audio = MP3(str(path))
    audio.add_tags()
    audio.save()
    return path


@pytest.fixture
def mp3_file(tmp_path: Path) -> Path:
    """Create a temporary MP3 file."""
    return _make_mp3(tmp_path / "test.mp3")


class TestWriteTags:
    """Tests for write_tags function."""

    def test_write_id3_tags_to_mp3(self, mp3_file: Path) -> None:
        """Write ID3 tags to an MP3 file and read them back."""
        tags = {
            "artist": "Test Artist",
            "title": "Test Title",
            "album": "Test Album",
            "year": "2024",
            "genre": "Electronic",
            "track_number": "3",
        }
        write_tags(str(mp3_file), tags)

        audio = MP3(str(mp3_file))
        assert audio.tags is not None
        assert str(audio.tags["TPE1"]) == "Test Artist"
        assert str(audio.tags["TIT2"]) == "Test Title"
        assert str(audio.tags["TALB"]) == "Test Album"

    def test_write_tags_handles_none_audio(self, tmp_path: Path) -> None:
        """write_tags raises ValueError for non-audio files."""
        bad_file = tmp_path / "not_audio.txt"
        bad_file.write_text("not audio")
        with pytest.raises(ValueError, match="not a recognized audio"):
            write_tags(str(bad_file), {"artist": "Test"})

    def test_write_tags_adds_tags_when_none(self, mp3_file: Path) -> None:
        """write_tags handles files with no existing tags by adding them."""
        # Remove existing tags
        audio = MP3(str(mp3_file))
        audio.delete()
        audio.save()

        # Now write tags
        write_tags(str(mp3_file), {"artist": "New Artist"})

        # Verify
        audio = MP3(str(mp3_file))
        assert audio.tags is not None
        assert str(audio.tags["TPE1"]) == "New Artist"

    def test_write_tags_none_leaves_absent_field_absent(self, mp3_file: Path) -> None:
        """A None value for an already-absent field is a harmless no-op (nothing to delete)."""
        write_tags(str(mp3_file), {"artist": "Test", "title": None})

        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TIT2" not in audio.tags

    def test_write_tags_none_deletes_existing_id3_frame(self, mp3_file: Path) -> None:
        """phaze-52qd: a None value DELETES an existing ID3 frame (the undo delete path)."""
        write_tags(str(mp3_file), {"artist": "Sven Vath", "album": "Coachella 2024"})
        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TALB" in audio.tags

        # Now re-apply a snapshot that marks both fields absent -- they must be removed.
        write_tags(str(mp3_file), {"artist": None, "album": None})
        audio = MP3(str(mp3_file))
        assert "TPE1" not in audio.tags
        assert "TALB" not in audio.tags


class TestWriteVorbisFormat:
    """Tests for Vorbis format writing via mock (OGG/FLAC/OPUS)."""

    def test_write_vorbis_keys(self) -> None:
        """Vorbis writer sets correct keys with list-wrapped values."""
        audio = MagicMock()
        _write_vorbis(audio, {"artist": "Vorbis Artist", "title": "Vorbis Title", "year": "2024"})

        audio.__setitem__.assert_any_call("artist", ["Vorbis Artist"])
        audio.__setitem__.assert_any_call("title", ["Vorbis Title"])
        audio.__setitem__.assert_any_call("date", ["2024"])

    def test_write_vorbis_skips_none(self) -> None:
        """Vorbis writer skips None values."""
        audio = MagicMock()
        _write_vorbis(audio, {"artist": "Test", "title": None})

        audio.__setitem__.assert_called_once_with("artist", ["Test"])

    def test_write_vorbis_track_number(self) -> None:
        """Vorbis writer maps track_number to 'tracknumber'."""
        audio = MagicMock()
        _write_vorbis(audio, {"track_number": "7"})

        audio.__setitem__.assert_called_once_with("tracknumber", ["7"])

    def test_write_vorbis_ignores_unmapped_field(self) -> None:
        """A field with no Vorbis key mapping is skipped entirely."""
        audio = MagicMock()
        _write_vorbis(audio, {"bogus_field": "value"})

        audio.__setitem__.assert_not_called()
        audio.__delitem__.assert_not_called()

    def test_write_vorbis_none_deletes_present_key(self) -> None:
        """A None value DELETES the key when it exists on the file (phaze-52qd undo semantics)."""
        audio = MagicMock()
        audio.__contains__.return_value = True
        _write_vorbis(audio, {"artist": None})

        audio.__delitem__.assert_called_once_with("artist")
        audio.__setitem__.assert_not_called()

    def test_write_vorbis_list_genre_writes_every_value_as_a_separate_comment(self) -> None:
        """phaze-z2u08: a list[str] genre (an undo snapshot) writes one comment PER value."""
        audio = MagicMock()
        _write_vorbis(audio, {"genre": ["House", "Techno"]})

        audio.__setitem__.assert_called_once_with("genre", ["House", "Techno"])


class TestWriteMP4Format:
    """Tests for MP4/M4A format writing via mock."""

    def test_write_mp4_keys(self) -> None:
        """MP4 writer sets correct atom keys."""
        audio = MagicMock()
        _write_mp4(audio, {"artist": "MP4 Artist", "album": "MP4 Album"})

        audio.__setitem__.assert_any_call("\xa9ART", ["MP4 Artist"])
        audio.__setitem__.assert_any_call("\xa9alb", ["MP4 Album"])

    def test_write_mp4_track_number_tuple(self) -> None:
        """MP4 writer uses [(track_number, 0)] tuple format for trkn."""
        audio = MagicMock()
        _write_mp4(audio, {"track_number": "5"})

        audio.__setitem__.assert_called_once_with("trkn", [(5, 0)])

    def test_write_mp4_skips_none(self) -> None:
        """MP4 writer skips None values."""
        audio = MagicMock()
        _write_mp4(audio, {"artist": "Test", "title": None})

        audio.__setitem__.assert_called_once_with("\xa9ART", ["Test"])

    def test_write_mp4_ignores_unmapped_field(self) -> None:
        """A field with no MP4 atom mapping is skipped entirely."""
        audio = MagicMock()
        _write_mp4(audio, {"bogus_field": "value"})

        audio.__setitem__.assert_not_called()
        audio.__delitem__.assert_not_called()

    def test_write_mp4_none_deletes_present_atom(self) -> None:
        """A None value DELETES the atom when it exists on the file (phaze-52qd undo semantics)."""
        audio = MagicMock()
        audio.__contains__.return_value = True
        _write_mp4(audio, {"artist": None})

        audio.__delitem__.assert_called_once_with("\xa9ART")
        audio.__setitem__.assert_not_called()

    def test_write_mp4_list_genre_writes_every_value_as_a_separate_atom_entry(self) -> None:
        """phaze-z2u08: a list[str] genre (an undo snapshot) writes every value into the atom."""
        audio = MagicMock()
        _write_mp4(audio, {"genre": ["House", "Techno"]})

        audio.__setitem__.assert_called_once_with("\xa9gen", ["House", "Techno"])


class TestMp4TrackTuple:
    """phaze-2zl7: the MP4 ``trkn`` atom writer must accept the raw "N/total" undo text."""

    def test_plain_int_writes_zero_total(self) -> None:
        """Every FORWARD write (compute_proposed_tags has no track-total source) is unchanged."""
        assert _mp4_track_tuple(5) == (5, 0)

    def test_raw_fraction_text_restores_the_total(self) -> None:
        assert _mp4_track_tuple("3/12") == (3, 12)

    def test_raw_text_without_a_slash_is_a_plain_number(self) -> None:
        assert _mp4_track_tuple("7") == (7, 0)


class TestWriteID3Format:
    """Direct-dispatch tests for the ID3 writer (the MP3 round-trips above cover the mapped fields)."""

    def test_write_id3_ignores_unmapped_field(self) -> None:
        """A field with no ID3 frame mapping is skipped entirely."""
        audio = MagicMock()
        _write_id3(audio, {"bogus_field": "value"})

        audio.tags.add.assert_not_called()
        audio.tags.delall.assert_not_called()

    def test_write_id3_list_genre_writes_a_single_tcon_frame_with_every_value(self) -> None:
        """phaze-z2u08: a list[str] genre (an undo snapshot) writes one TCON with every value."""
        audio = MagicMock()
        _write_id3(audio, {"genre": ["House", "Techno"]})

        args, _kwargs = audio.tags.add.call_args
        frame = args[0]
        assert list(frame.text) == ["House", "Techno"]


class TestVerifyWrite:
    """Tests for verify_write function."""

    def test_perfect_write_returns_empty(self, mp3_file: Path) -> None:
        """verify_write returns empty dict when written tags match."""
        tags = {"artist": "Test Artist", "title": "Test Title"}
        write_tags(str(mp3_file), tags)
        discrepancies = verify_write(str(mp3_file), tags)
        assert discrepancies == {}

    def test_discrepancy_detected(self, mp3_file: Path) -> None:
        """verify_write detects mismatched tags."""
        write_tags(str(mp3_file), {"artist": "Actual Artist"})
        discrepancies = verify_write(str(mp3_file), {"artist": "Expected Artist"})
        assert "artist" in discrepancies
        assert discrepancies["artist"]["expected"] == "Expected Artist"
        assert discrepancies["artist"]["actual"] == "Actual Artist"

    def test_verify_none_expected_passes_when_field_absent(self, mp3_file: Path) -> None:
        """An expected None for an absent field is NOT a discrepancy (a deletion that held)."""
        write_tags(str(mp3_file), {"artist": "Test"})
        discrepancies = verify_write(str(mp3_file), {"artist": "Test", "title": None})
        assert discrepancies == {}

    def test_verify_none_expected_flags_surviving_field(self, mp3_file: Path) -> None:
        """phaze-52qd: an expected None is a discrepancy when the field is still on disk.

        This is what makes an undo that FAILED to delete an added tag report a real discrepancy
        instead of a false 'completed' reversal.
        """
        write_tags(str(mp3_file), {"artist": "Should Be Deleted"})
        discrepancies = verify_write(str(mp3_file), {"artist": None})
        assert "artist" in discrepancies
        assert discrepancies["artist"]["expected"] is None
        assert discrepancies["artist"]["actual"] == "Should Be Deleted"

    def test_semantic_field_match_via_normalizer_is_not_a_discrepancy(self, mp3_file: Path) -> None:
        """phaze-2zl7: a raw "N/total" write compares equal to its own normalized total-drop.

        write_tags/verify_write both see the RAW undo text "3/12"; the normalizer used for
        year/track_number must find this equal to the on-disk normalized track number (3), not
        report a false discrepancy just because the raw string differs from the parsed int.
        """
        write_tags(str(mp3_file), {"track_number": "3/12"})
        discrepancies = verify_write(str(mp3_file), {"track_number": "3/12"})
        assert discrepancies == {}

    def test_semantic_field_mismatch_is_reported_through_the_normalizer(self, mp3_file: Path) -> None:
        """phaze-2zl7: a genuine track_number mismatch is still caught through the normalized
        comparator (_SEMANTIC_COMPARE_FIELDS), not silently accepted because it's a "special"
        field.
        """
        write_tags(str(mp3_file), {"track_number": "3/12"})
        discrepancies = verify_write(str(mp3_file), {"track_number": "9/12"})
        assert "track_number" in discrepancies
        assert discrepancies["track_number"]["expected"] == "9/12"
        # "actual" is the extractor's own normalized parse (a bare int), not the raw on-disk
        # text -- verify_write re-reads through extract_tags, which drops the total.
        assert discrepancies["track_number"]["actual"] == "3"

    def test_list_genre_match_against_the_raw_multi_value_re_read_is_not_a_discrepancy(self, mp3_file: Path) -> None:
        """phaze-z2u08: a list[str] expected genre compares against the re-read's raw multi-value
        list, not the single-value normalized ``genre`` field (which is always just the first
        entry and would otherwise report every faithful multi-value write as a false mismatch).
        """
        write_tags(str(mp3_file), {"genre": ["House", "Techno"]})
        discrepancies = verify_write(str(mp3_file), {"genre": ["House", "Techno"]})
        assert discrepancies == {}

    def test_list_genre_mismatch_is_still_reported(self, mp3_file: Path) -> None:
        """A genuine multi-value genre mismatch is still caught, not silently accepted."""
        write_tags(str(mp3_file), {"genre": ["House", "Trance"]})
        discrepancies = verify_write(str(mp3_file), {"genre": ["House", "Techno"]})
        assert "genre" in discrepancies
        assert discrepancies["genre"]["expected"] == "House; Techno"
        assert discrepancies["genre"]["actual"] == "House; Trance"


class TestExtractBeforeTags:
    """phaze-52qd: the before/undo snapshot must span every core field, marking absent tags None."""

    def test_records_absent_fields_as_none(self, mp3_file: Path) -> None:
        """A previously-untagged file yields an all-None snapshot -- not an empty dict.

        Pre-fix this returned {} (only non-None fields), so undo had nothing to delete and the
        tags a write ADDED survived the 'revert'.
        """
        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot == {
            "artist": None,
            "title": None,
            "album": None,
            "year": None,
            "genre": None,
            "track_number": None,
        }

    def test_records_present_and_absent_together(self, mp3_file: Path) -> None:
        """Present fields keep their values; absent fields are explicit None."""
        write_tags(str(mp3_file), {"artist": "Present Artist"})
        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot["artist"] == "Present Artist"
        assert snapshot["album"] is None
        assert set(snapshot) == {"artist", "title", "album", "year", "genre", "track_number"}


class TestExtractBeforeTagsRawFidelity:
    """phaze-2zl7: year/track_number/genre must snapshot the RAW on-disk text, not the normalized
    (search-oriented) parse -- a full release date, a track's total, and every genre value.

    Exercised through :func:`phaze.services.tag_write_disk._extract_before_tags` directly (the
    control plane's ``_extract_before_tags`` re-export is only ever CALLED here on the agent, via
    ``write_and_verify_sync`` -- see ``tests/review/tasks/test_tag_write.py`` for the end-to-end
    undo round trip through the actual agent task).
    """

    def test_track_number_preserves_the_total(self, mp3_file: Path) -> None:
        """ "3/12" must round-trip whole, not truncate to "3" (extract_tags._parse_track's rule)."""
        audio = ID3(str(mp3_file))
        audio.add(TRCK(encoding=3, text=["3/12"]))
        audio.save()

        snapshot = _extract_before_tags_disk(str(mp3_file))
        assert snapshot["track_number"] == "3/12"

    def test_year_preserves_the_full_release_date(self, mp3_file: Path) -> None:
        """ "2024-03-15" must round-trip whole, not truncate to 2024 (extract_tags._parse_year's rule)."""
        audio = ID3(str(mp3_file))
        audio.add(TDRC(encoding=3, text=["2024-03-15"]))
        audio.save()

        snapshot = _extract_before_tags_disk(str(mp3_file))
        assert snapshot["year"] == "2024-03-15"

    def test_genre_preserves_every_value_not_just_the_first(self, mp3_file: Path) -> None:
        """A multi-value TCON frame must round-trip all of it, not collapse to _first_str's first.

        phaze-z2u08: the snapshot keeps every value as a real ``list[str]`` -- not a "; "-joined
        string -- because nothing on the write side ever split a joined string back apart, so a
        single-value undo of that string collapsed the multi-value tag into one value on restore.
        """
        audio = ID3(str(mp3_file))
        audio.add(TCON(encoding=3, text=["House", "Techno"]))
        audio.save()

        snapshot = _extract_before_tags_disk(str(mp3_file))
        assert snapshot["genre"] == ["House", "Techno"]

    def test_single_value_genre_still_snapshots_as_a_plain_string(self, mp3_file: Path) -> None:
        """phaze-z2u08: a single-value genre is unaffected -- still a plain str, not a 1-item list."""
        audio = ID3(str(mp3_file))
        audio.add(TCON(encoding=3, text=["House"]))
        audio.save()

        snapshot = _extract_before_tags_disk(str(mp3_file))
        assert snapshot["genre"] == "House"

    def test_absent_fields_still_fall_back_to_none(self, mp3_file: Path) -> None:
        """No raw text on disk -> the normalized (also None) value is used, not a stray raw value."""
        snapshot = _extract_before_tags_disk(str(mp3_file))
        assert snapshot["year"] is None
        assert snapshot["track_number"] is None
        assert snapshot["genre"] is None


class TestUndoDeletesAddedTags:
    """phaze-52qd end-to-end: reverting a write that ADDED tags removes them from disk."""

    def test_undo_snapshot_removes_added_tags(self, mp3_file: Path) -> None:
        """Write artist+album into an untagged file, then re-apply the before snapshot to revert.

        The before snapshot (all-None for the untagged file) must delete both added frames and the
        reversal must verify COMPLETED, not silently leave the tags on disk.

        phaze-6bkk: exercised through ``write_and_verify_sync`` -- the on-disk sequence the AGENT
        runs. It used to go through ``execute_tag_write``, but that function no longer touches a
        file: under DIST-01 the api container has no media mount, so the write was moved to the
        owning agent's meta lane. The undo SEMANTICS under test (a None-valued snapshot DELETES the
        frame a write added) are unchanged and still live here.
        """
        # Untagged file -> capture the true before snapshot (all None).
        before = _extract_before_tags(str(mp3_file))

        status, _disc, _err, _before = write_and_verify_sync(str(mp3_file), {"artist": "Sven Vath", "album": "Coachella 2024"})
        assert status == TagWriteStatus.COMPLETED
        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TALB" in audio.tags

        # Undo re-applies the captured before snapshot.
        undo_status, _disc2, _err2, _before2 = write_and_verify_sync(str(mp3_file), before)

        assert undo_status == TagWriteStatus.COMPLETED
        audio = MP3(str(mp3_file))
        assert "TPE1" not in audio.tags
        assert "TALB" not in audio.tags

    def test_undo_restores_raw_track_and_date_fidelity(self, mp3_file: Path) -> None:
        """phaze-2zl7 end-to-end: undo restores the FULL track total and release date -- not the
        normalized 4-digit year / truncated track number -- and does not falsely report a
        discrepancy for a faithful raw-text restore.
        """
        audio = ID3(str(mp3_file))
        audio.add(TRCK(encoding=3, text=["3/12"]))
        audio.add(TDRC(encoding=3, text=["2024-03-15"]))
        audio.save()

        status, _disc, _err, before = write_and_verify_sync(str(mp3_file), {"artist": "New Artist"})
        assert status == TagWriteStatus.COMPLETED
        assert before["track_number"] == "3/12"
        assert before["year"] == "2024-03-15"

        undo_status, undo_disc, _err2, _before2 = write_and_verify_sync(str(mp3_file), before)
        assert undo_status == TagWriteStatus.COMPLETED, f"undo must not report a discrepancy for a faithful raw-text restore: {undo_disc}"

        audio = MP3(str(mp3_file))
        assert str(audio.tags["TRCK"]) == "3/12", "the track TOTAL must survive the undo round trip"
        assert str(audio.tags["TDRC"]) == "2024-03-15", "the full release date must survive the undo round trip"

    def test_undo_restores_multi_value_genre_as_separate_values_not_one_joined_string(self, mp3_file: Path) -> None:
        """phaze-z2u08 end-to-end: undo of a multi-value genre restores every value separately.

        Pre-fix, the snapshot joined ["House", "Techno"] into "House; Techno" and every write path
        wrote that back as ONE tag value. verify_write was structurally blind to the collapse (its
        re-read's first-value comparison matched the joined text), so the undo reported COMPLETED
        with zero discrepancies while silently corrupting the file's tags.
        """
        audio = ID3(str(mp3_file))
        audio.add(TCON(encoding=3, text=["House", "Techno"]))
        audio.save()

        status, _disc, _err, before = write_and_verify_sync(str(mp3_file), {"genre": "Trance"})
        assert status == TagWriteStatus.COMPLETED
        assert before["genre"] == ["House", "Techno"]

        undo_status, undo_disc, _err2, _before2 = write_and_verify_sync(str(mp3_file), before)
        assert undo_status == TagWriteStatus.COMPLETED, f"undo must not report a discrepancy for a faithful multi-value restore: {undo_disc}"

        audio = MP3(str(mp3_file))
        assert list(audio.tags["TCON"].text) == ["House", "Techno"], (
            "both original genre values must survive the undo round trip, not one joined value"
        )

    def test_verify_raises_on_unreadable_file(self, tmp_path: Path) -> None:
        """phaze-vq3g: an unreadable/absent file on re-read raises TagReadError, not a false discrepancy.

        Pre-fix, verify_write re-read via the SWALLOWING ``extract_tags``, so an I/O failure produced
        an all-field ``actual=None`` discrepancy indistinguishable from a genuinely-wrong write.
        """
        from phaze.services.metadata import TagReadError

        missing = tmp_path / "gone.mp3"
        with pytest.raises(TagReadError):
            verify_write(str(missing), {"artist": "Written Value"})

    def test_verify_absent_tags_is_a_real_discrepancy_not_a_read_failure(self, mp3_file: Path) -> None:
        """phaze-vq3g: a file that opens cleanly but LACKS the tag is a real discrepancy (actual=None).

        This is the case that must stay a discrepancy -- the distinction the fix draws is between a
        re-read that FAILED (raises) and tags that are genuinely absent (readable, reported None).
        """
        # Write nothing; the freshly-tagged (empty) file is fully readable but has no artist frame.
        discrepancies = verify_write(str(mp3_file), {"artist": "Expected"})
        assert "artist" in discrepancies
        assert discrepancies["artist"]["actual"] is None


class TestEnqueueTagWrite:
    """phaze-6bkk: ``enqueue_tag_write`` creates the audit row and DISPATCHES -- it writes nothing.

    The old ``execute_tag_write`` performed the mutagen write inline in the api process. That could
    never work in the documented production topology: DIST-01 gives the api container no media
    mount, so ``current_path`` (a file-SERVER path) did not exist there and every write failed
    ``[Errno 2]`` behind a toast blaming file permissions. The disk half now lives in
    ``phaze.services.tag_write_disk`` / ``phaze.tasks.tag_write`` and is tested there; what remains
    here is the control-plane contract: guard, ``queued`` audit row, lane routing, the
    dispatch-failure path, the phaze-ysnp write-ahead commit ordering, and the phaze-lwqk per-file
    mutual-exclusion guard.

    READ-05 / D-01: the guard gates on ``await is_applied(session, file_record.id)`` -- a real DB
    ``EXISTS`` over ``proposals.status == 'executed'`` -- NOT on ``file_record.state``.

    phaze-lwqk/phaze-ysnp: ``enqueue_tag_write`` now does real DB work of its own (an advisory-lock
    acquire, an existence check, and a commit) BEFORE dispatching, so the "dispatch contract" cases
    below use a REAL DB-backed ``FileRecord``/session (``make_file`` + the hermetic ``session``
    fixture) instead of a bare ``AsyncMock`` session -- a mock can't satisfy those real queries (nor
    the ``tag_write_log.file_id`` foreign key the QUEUED insert now actually commits).
    """

    # ------------------------------------------------------------------------------------------------
    # SC#2 guard behavior (real DB rows, mutation-checked) -- the load-bearing behavior-revival test.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_applied_file_passes_guard(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """SC#2: an actually-applied file (executed proposal, ``state != 'executed'``) PASSES the guard.

        Mutation check: reverting the guard to ``file_record.state != EXECUTED`` makes this fixture
        (applied-ness via proposals.status) RAISE and this test go RED.
        """
        file = await make_file()
        await _add_proposal(session, file.id, ProposalStatus.EXECUTED.value)
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock()

        log_entry = await enqueue_tag_write(session, router, file, {"artist": "New Artist"}, "tracklist")

        # The guard admitted the file and the dispatch path ran to completion.
        assert log_entry.status == TagWriteStatus.QUEUED
        assert log_entry.file_id == file.id
        router.enqueue_for_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_applied_file_raises(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A file with no executed proposal (only a failed one) RAISES ``ValueError`` matching 'executed'."""
        file = await make_file()
        await _add_proposal(session, file.id, ProposalStatus.FAILED.value)
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock()

        with pytest.raises(ValueError, match="executed"):
            await enqueue_tag_write(session, router, file, {"artist": "Test"}, "tracklist")

        router.enqueue_for_agent.assert_not_awaited()

    # ------------------------------------------------------------------------------------------------
    # Dispatch contract -- the guard is explicitly admitted so routing is exercised in isolation.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_creates_queued_tag_write_log(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The audit row is created up front in ``queued``, with an EMPTY before-tags snapshot.

        ``before_tags`` can only be read where the file is, so it stays empty until the agent's
        callback fills it -- the row never claims a pre-write state the api did not observe.
        """
        fr = await make_file()
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "New Artist"}, "tracklist")

        assert log_entry.status == TagWriteStatus.QUEUED
        assert log_entry.source == "tracklist"
        assert log_entry.after_tags == {"artist": "New Artist"}
        assert log_entry.before_tags == {}
        assert log_entry.error_message is None

    @pytest.mark.asyncio
    async def test_routes_to_the_owning_agent_with_current_path(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """phaze-c9w9 affinity + the D-24 ``current_path`` exception, both asserted on the payload.

        The write MUST go to ``file_record.agent_id`` -- the agent that reported the file -- and MUST
        carry ``current_path``, because a tag write is only offered for an APPLIED file whose
        ``original_path`` no longer names anything on disk.
        """
        fr = await make_file()
        fr.current_path = "/data/music/<set-02>.mp3"
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "tracklist")

        kwargs = router.enqueue_for_agent.await_args.kwargs
        assert kwargs["task_name"] == "write_file_tags"
        assert kwargs["agent_id"] == fr.agent_id
        payload = kwargs["payload"]
        assert payload.file_path == "/data/music/<set-02>.mp3"
        assert payload.agent_id == fr.agent_id
        assert payload.tags == {"artist": "Test"}
        # The pre-minted log id is what makes the agent's callback retry-stable.
        assert payload.log_id == log_entry.id

    @pytest.mark.asyncio
    async def test_write_file_tags_routes_to_the_meta_lane(self) -> None:
        """The dispatched task name resolves to the ``meta`` lane -- the rw-mounted worker."""
        from phaze.services.enqueue_router import lane_for_task

        assert lane_for_task("write_file_tags") == "meta"

    @pytest.mark.asyncio
    async def test_enqueue_failure_downgrades_the_row_to_failed(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """An UNAMBIGUOUS broker/enqueue failure leaves a FAILED row with an actionable message.

        A plain exception out of ``enqueue_for_agent`` here stands in for a failure the real
        ``AgentTaskRouter`` would raise BEFORE ``queue.enqueue()`` is ever called (e.g.
        ``queue.connect()``) -- provably nothing was created, so downgrading is safe. A ``queued``
        row no agent will ever answer for would hold the file out of every terminal count forever
        with nothing on screen explaining why. Contrast
        :meth:`test_ambiguous_enqueue_failure_leaves_the_row_queued` below.
        """
        fr = await make_file()
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock(side_effect=RuntimeError("broker unreachable"))

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "manual_edit")

        assert log_entry.status == TagWriteStatus.FAILED
        assert log_entry.error_message is not None
        assert fr.agent_id in log_entry.error_message
        assert "broker unreachable" in log_entry.error_message

    @pytest.mark.asyncio
    async def test_ambiguous_enqueue_failure_leaves_the_row_queued(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """phaze-9f82r: an AMBIGUOUS enqueue failure must NOT downgrade to FAILED.

        ``AmbiguousEnqueueError`` means ``queue.enqueue()`` raised AFTER the broker connection was
        live -- the ``saq_jobs`` INSERT (and its ledger row) may already be durably committed even
        though this call never got its ack. Downgrading here would let an operator retry mint a
        SECOND queued row + job for the SAME file while a possibly-live ghost job from THIS attempt
        is still queued -- two concurrent mutagen rewrites of one archive file. The row must stay
        ``queued`` so the phaze-lwqk guard refuses a retry instead.
        """
        fr = await make_file()
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock(side_effect=AmbiguousEnqueueError("enqueue raised after connect"))

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "manual_edit")

            assert log_entry.status == TagWriteStatus.QUEUED
            assert log_entry.error_message is None

            # The phaze-lwqk guard now refuses a retry for this file -- exactly the protection an
            # incorrect FAILED downgrade would have bypassed.
            with pytest.raises(TagWriteAlreadyQueuedError):
                await enqueue_tag_write(session, router, fr, {"artist": "Retry"}, "manual_edit")

    @pytest.mark.asyncio
    async def test_does_not_touch_the_filesystem(self, session: AsyncSession, make_file, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """The DIST-01 regression guard: a nonexistent path is NOT an error on the control plane.

        Pre-fix this exact call did ``mutagen.File("/data/music/...")`` inside the api container and
        recorded FAILED for every file forever. Post-fix the api only records intent and hands off,
        so a path it cannot see is irrelevant to it.
        """
        fr = await make_file()
        fr.current_path = str(tmp_path / "does-not-exist.mp3")
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "tracklist")

        assert log_entry.status == TagWriteStatus.QUEUED
        assert log_entry.error_message is None

    # ------------------------------------------------------------------------------------------------
    # phaze-ysnp: the QUEUED row must be DURABLE before the dispatch, not merely flushed.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_queued_row_is_committed_before_the_dispatch(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """phaze-ysnp: the write-ahead commit must happen BEFORE ``enqueue_for_agent`` is awaited.

        ``enqueue_for_agent`` hands the job to a REAL external system (the agent's SAQ queue, a
        separate Postgres connection/transaction from ``session``) that can start running before
        this transaction would otherwise have committed. Without committing first, a crash between
        the flush and the caller's own commit would strand an already-dispatched job with no
        durable row for the agent's callback to PATCH.
        """
        fr = await make_file()
        events: list[str] = []
        real_commit = session.commit

        async def _tracked_commit() -> None:
            events.append("commit")
            await real_commit()

        router = MagicMock()

        async def _tracked_enqueue(**_kwargs: object) -> None:
            events.append("enqueue")

        router.enqueue_for_agent = _tracked_enqueue

        with (
            patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)),
            patch.object(session, "commit", _tracked_commit),
        ):
            await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "tracklist")

        assert "commit" in events
        assert "enqueue" in events
        assert events.index("commit") < events.index("enqueue"), f"commit must precede the dispatch, got order {events}"

    # ------------------------------------------------------------------------------------------------
    # phaze-lwqk: per-file mutual exclusion -- a second concurrent dispatch for the SAME file refuses.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_second_enqueue_for_an_already_queued_file_raises(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A file with an unresolved ``queued`` row refuses a second dispatch.

        Without this guard, a double-click / two tabs / a per-file write racing the bulk loop's
        dispatch of the SAME file could queue two ``write_file_tags`` jobs -- two concurrent mutagen
        full-file rewrites on the agent, a real corruption risk for an irreplaceable archive file.
        """
        fr = await make_file()
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            first = await enqueue_tag_write(session, router, fr, {"artist": "First"}, "tracklist")
            assert first.status == TagWriteStatus.QUEUED

            with pytest.raises(TagWriteAlreadyQueuedError):
                await enqueue_tag_write(session, router, fr, {"artist": "Second"}, "tracklist")

        # Only the first dispatch actually reached the agent router.
        router.enqueue_for_agent.assert_awaited_once()
        rows = (await session.execute(select(TagWriteLog).where(TagWriteLog.file_id == fr.id))).scalars().all()
        assert len(rows) == 1, "the refused second call must not create a second row"

    @pytest.mark.asyncio
    async def test_enqueue_succeeds_again_once_the_prior_write_is_terminal(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """Once the prior ``queued`` row resolves to a terminal status, a new dispatch is allowed."""
        fr = await make_file()
        router = MagicMock()
        router.enqueue_for_agent = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            first = await enqueue_tag_write(session, router, fr, {"artist": "First"}, "tracklist")

        # Simulate the agent's callback resolving the row to a terminal status.
        first.status = TagWriteStatus.COMPLETED.value
        await session.commit()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            second = await enqueue_tag_write(session, router, fr, {"artist": "Second"}, "tracklist")

        assert second.status == TagWriteStatus.QUEUED
        assert router.enqueue_for_agent.await_count == 2
