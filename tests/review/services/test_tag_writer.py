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
from phaze.services.tag_writer import (
    _extract_before_tags,
    _mp4_track_tuple,
    _write_mp4,
    _write_vorbis,
    execute_tag_write,
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
    """

    def test_track_number_preserves_the_total(self, mp3_file: Path) -> None:
        """ "3/12" must round-trip whole, not truncate to "3" (extract_tags._parse_track's rule)."""
        audio = ID3(str(mp3_file))
        audio.add(TRCK(encoding=3, text=["3/12"]))
        audio.save()

        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot["track_number"] == "3/12"

    def test_year_preserves_the_full_release_date(self, mp3_file: Path) -> None:
        """ "2024-03-15" must round-trip whole, not truncate to 2024 (extract_tags._parse_year's rule)."""
        audio = ID3(str(mp3_file))
        audio.add(TDRC(encoding=3, text=["2024-03-15"]))
        audio.save()

        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot["year"] == "2024-03-15"

    def test_genre_preserves_every_value_not_just_the_first(self, mp3_file: Path) -> None:
        """A multi-value TCON frame must round-trip all of it, not collapse to _first_str's first."""
        audio = ID3(str(mp3_file))
        audio.add(TCON(encoding=3, text=["House", "Techno"]))
        audio.save()

        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot["genre"] == "House; Techno"

    def test_absent_fields_still_fall_back_to_none(self, mp3_file: Path) -> None:
        """No raw text on disk -> the normalized (also None) value is used, not a stray raw value."""
        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot["year"] is None
        assert snapshot["track_number"] is None
        assert snapshot["genre"] is None


class TestMp4TrackTuple:
    """phaze-2zl7: the MP4 ``trkn`` atom writer must accept the raw "N/total" undo text."""

    def test_plain_int_writes_zero_total(self) -> None:
        """Every FORWARD write (compute_proposed_tags has no track-total source) is unchanged."""
        assert _mp4_track_tuple(5) == (5, 0)

    def test_raw_fraction_text_restores_the_total(self) -> None:
        assert _mp4_track_tuple("3/12") == (3, 12)

    def test_raw_text_without_a_slash_is_a_plain_number(self) -> None:
        assert _mp4_track_tuple("7") == (7, 0)


class TestUndoDeletesAddedTags:
    """phaze-52qd end-to-end: reverting a write that ADDED tags removes them from disk."""

    @pytest.mark.asyncio
    async def test_undo_snapshot_removes_added_tags(self, session: AsyncSession, make_file, mp3_file: Path) -> None:  # type: ignore[no-untyped-def]
        """Write artist+album into an untagged file, then re-apply the before snapshot to revert.

        The before snapshot (all-None for the untagged file) must delete both added frames and the
        reversal must verify COMPLETED, not silently leave the tags on disk.

        phaze-ysnp: execute_tag_write now COMMITS a write-ahead marker for real, so this needs a
        real DB-backed ``FileRecord`` for the ``file_id`` foreign key.
        """
        # Untagged file -> capture the true before snapshot (all None).
        before = _extract_before_tags(str(mp3_file))

        fr = await make_file()
        fr.current_path = str(mp3_file)

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            write_log = await execute_tag_write(session, fr, {"artist": "Sven Vath", "album": "Coachella 2024"}, "tracklist")
        assert write_log.status == TagWriteStatus.COMPLETED
        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TALB" in audio.tags

        # Undo re-applies the captured before snapshot (source="undo").
        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            undo_log = await execute_tag_write(session, fr, before, "undo")

        assert undo_log.status == TagWriteStatus.COMPLETED
        audio = MP3(str(mp3_file))
        assert "TPE1" not in audio.tags
        assert "TALB" not in audio.tags

    @pytest.mark.asyncio
    async def test_undo_restores_raw_track_and_date_fidelity(self, session: AsyncSession, make_file, mp3_file: Path) -> None:  # type: ignore[no-untyped-def]
        """phaze-2zl7 end-to-end: undo restores the FULL track total and release date -- not the
        normalized 4-digit year / truncated track number -- and does not falsely toast a discrepancy.
        """
        audio = ID3(str(mp3_file))
        audio.add(TRCK(encoding=3, text=["3/12"]))
        audio.add(TDRC(encoding=3, text=["2024-03-15"]))
        audio.save()

        fr = await make_file()
        fr.current_path = str(mp3_file)

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            write_log = await execute_tag_write(session, fr, {"artist": "New Artist"}, "tracklist")
        assert write_log.status == TagWriteStatus.COMPLETED
        assert write_log.before_tags["track_number"] == "3/12"
        assert write_log.before_tags["year"] == "2024-03-15"

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            undo_log = await execute_tag_write(session, fr, write_log.before_tags, "undo")

        assert undo_log.status == TagWriteStatus.COMPLETED, (
            f"undo must not report a discrepancy for a faithful raw-text restore: {undo_log.discrepancies}"
        )
        audio = MP3(str(mp3_file))
        assert str(audio.tags["TRCK"]) == "3/12", "the track TOTAL must survive the undo round trip"
        assert str(audio.tags["TDRC"]) == "2024-03-15", "the full release date must survive the undo round trip"

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


class TestExecuteTagWrite:
    """Tests for execute_tag_write async function.

    READ-05 / D-01: the guard at ``tag_writer.py`` now gates on ``await is_applied(session,
    file_record.id)`` -- a real DB ``EXISTS`` over ``proposals.status == 'executed'`` -- NOT on
    ``file_record.state``. The guard behavior cases (SC#2) therefore seed REAL rows against the test
    DB; the write-mechanics cases patch ``is_applied`` to explicitly admit the file so they exercise
    the mutagen path in isolation.
    """

    # ------------------------------------------------------------------------------------------------
    # SC#2 guard behavior (real DB rows, mutation-checked) -- the load-bearing behavior-revival test.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_applied_file_passes_guard(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """SC#2: an actually-applied file (executed proposal, ``state != 'executed'``) PASSES the guard.

        This is the behavior the phase revives: pre-Phase-85 the guard read ``file_record.state !=
        the EXECUTED scalar state and ALWAYS failed (no ``src/`` writer produced that scalar state).
        The file's own ``state`` is deliberately ``'moved'`` -- the real apply-path outcome -- proving
        the guard admits on ``proposals.status == 'executed'`` alone.

        Mutation check (recorded in SUMMARY): reverting the guard to ``file_record.state !=
        the EXECUTED scalar state makes this fixture (applied-ness via proposals.status) RAISE and this test go RED.
        """
        file = await make_file()
        await _add_proposal(session, file.id, ProposalStatus.EXECUTED.value)

        with (
            patch("phaze.services.tag_writer._extract_before_tags", return_value={}),
            patch("phaze.services.tag_writer.write_tags"),
            patch("phaze.services.tag_writer.verify_write", return_value={}),
        ):
            log_entry = await execute_tag_write(session, file, {"artist": "New Artist"}, "tracklist")

        # The guard admitted the file and the write path ran to completion.
        assert log_entry.status == TagWriteStatus.COMPLETED
        assert log_entry.file_id == file.id

    @pytest.mark.asyncio
    async def test_non_applied_file_raises(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A file with no executed proposal (only a failed one) RAISES ``ValueError`` matching 'executed'."""
        file = await make_file()
        await _add_proposal(session, file.id, ProposalStatus.FAILED.value)

        with pytest.raises(ValueError, match="executed"):
            await execute_tag_write(session, file, {"artist": "Test"}, "tracklist")

    # ------------------------------------------------------------------------------------------------
    # Write-mechanics cases -- guard explicitly admitted so the mutagen path is exercised in isolation.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_creates_tag_write_log_on_success(self, session: AsyncSession, make_file, mp3_file: Path) -> None:  # type: ignore[no-untyped-def]
        """execute_tag_write creates a TagWriteLog entry on successful write.

        phaze-ysnp: execute_tag_write now COMMITS a write-ahead marker for real (not just
        ``session.flush()``), so this needs a real DB-backed ``FileRecord`` for the ``file_id``
        foreign key -- a bare mock session/file_record can no longer stand in.
        """
        fr = await make_file()
        fr.current_path = str(mp3_file)

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await execute_tag_write(session, fr, {"artist": "New Artist"}, "tracklist")

        assert log_entry.status == TagWriteStatus.COMPLETED
        assert log_entry.source == "tracklist"
        assert log_entry.after_tags == {"artist": "New Artist"}
        assert isinstance(log_entry.before_tags, dict)
        assert log_entry.id is not None

    @pytest.mark.asyncio
    async def test_creates_failed_log_on_error(self, session: AsyncSession, make_file, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        """execute_tag_write creates a FAILED log entry when write errors."""
        bad_path = tmp_path / "nonexistent.mp3"
        fr = await make_file()
        fr.current_path = str(bad_path)

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await execute_tag_write(session, fr, {"artist": "Test"}, "manual_edit")

        assert log_entry.status == TagWriteStatus.FAILED
        assert log_entry.error_message is not None

    @pytest.mark.asyncio
    async def test_uses_current_path_not_original(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """execute_tag_write uses file_record.current_path."""
        fr = await make_file()
        fr.current_path = "/dest/music.mp3"

        with (
            patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)),
            patch("phaze.services.tag_writer.extract_tags") as mock_extract,
            patch("phaze.services.tag_writer.write_tags") as mock_write,
            patch("phaze.services.tag_writer.verify_write", return_value={}),
        ):
            mock_extract.return_value = MagicMock(
                artist=None,
                title=None,
                album=None,
                year=None,
                genre=None,
                track_number=None,
                raw_year=None,
                raw_track_number=None,
                raw_genre=None,
            )
            await execute_tag_write(session, fr, {"artist": "Test"}, "tracklist")
            mock_write.assert_called_once_with("/dest/music.mp3", {"artist": "Test"})

    @pytest.mark.asyncio
    async def test_discrepancy_status(self, session: AsyncSession, make_file, mp3_file: Path) -> None:  # type: ignore[no-untyped-def]
        """execute_tag_write returns DISCREPANCY status when verify finds mismatches."""
        fr = await make_file()
        fr.current_path = str(mp3_file)

        with (
            patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)),
            patch("phaze.services.tag_writer.verify_write", return_value={"artist": {"expected": "A", "actual": "B"}}),
        ):
            log_entry = await execute_tag_write(session, fr, {"artist": "A"}, "tracklist")
            assert log_entry.status == TagWriteStatus.DISCREPANCY
            assert log_entry.discrepancies == {"artist": {"expected": "A", "actual": "B"}}

    @pytest.mark.asyncio
    async def test_verify_read_failure_records_verify_failed_not_discrepancy(self, session: AsyncSession, make_file, mp3_file: Path) -> None:  # type: ignore[no-untyped-def]
        """phaze-vq3g: a LANDED write whose verify re-read fails is audited VERIFY_FAILED, not DISCREPANCY.

        ``write_tags`` succeeds (patched no-op) but the verify re-read raises ``TagReadError``. The
        audit row must record the distinct VERIFY_FAILED status with an explanatory error_message and
        NO synthesized all-field ``actual=None`` discrepancy -- the on-disk tags are correct, only the
        confirmation read failed.
        """
        from phaze.services.metadata import TagReadError

        fr = await make_file()
        fr.current_path = str(mp3_file)

        with (
            patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)),
            patch("phaze.services.tag_writer.write_tags"),
            patch("phaze.services.tag_writer.verify_write", side_effect=TagReadError("mount hiccup on re-read")),
        ):
            log_entry = await execute_tag_write(session, fr, {"artist": "A"}, "tracklist")

        assert log_entry.status == TagWriteStatus.VERIFY_FAILED
        assert log_entry.discrepancies is None
        assert log_entry.error_message is not None
        assert "verify failed" in log_entry.error_message

    @pytest.mark.asyncio
    async def test_write_ahead_marker_survives_a_failure_after_it_commits(self, session: AsyncSession, make_file, mp3_file: Path) -> None:  # type: ignore[no-untyped-def]
        """phaze-ysnp: a failure AFTER the write-ahead marker's own commit must not lose the row.

        Simulates a crash/DB-connectivity drop between the durable IN_PROGRESS commit and the final
        status update: the SECOND ``session.flush()`` call (the one persisting the terminal status)
        raises. The caller's own rollback (mirroring the bulk loop's per-file ``except``/
        ``rollback``) must not undo the ALREADY-COMMITTED marker -- an orphaned IN_PROGRESS row is
        the honest, self-healing outcome the fix exists to produce, instead of the write vanishing
        from the append-only audit trail entirely.
        """
        fr = await make_file()
        fr.current_path = str(mp3_file)
        # Capture BEFORE the rollback below expires this ORM instance (phaze-o2ln's own lesson).
        file_id = fr.id

        flaky_flush = AsyncMock(side_effect=[None, OSError("simulated DB connectivity drop")])

        with (
            patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)),
            patch.object(session, "flush", flaky_flush),
            pytest.raises(OSError, match="simulated DB connectivity drop"),
        ):
            await execute_tag_write(session, fr, {"artist": "New Artist"}, "tracklist")

        # Mirror the bulk loop's per-file recovery (phaze-k7g6): roll back only the uncommitted
        # tail of work, exactly like `bulk_write_no_discrepancies`'s per-file `except Exception`.
        await session.rollback()

        rows = (await session.execute(select(TagWriteLog).where(TagWriteLog.file_id == file_id))).scalars().all()
        assert len(rows) == 1, "the write-ahead marker must survive the caller's rollback"
        assert rows[0].status == TagWriteStatus.IN_PROGRESS.value
        assert rows[0].before_tags == dict.fromkeys(("artist", "title", "album", "year", "genre", "track_number")), (
            "the untagged fixture file's before-snapshot is every core field, explicitly None"
        )
        assert rows[0].after_tags == {"artist": "New Artist"}
