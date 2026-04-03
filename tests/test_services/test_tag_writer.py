"""Tests for the tag writer service."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from mutagen.mp3 import MP3
import pytest

from phaze.models.file import FileState
from phaze.models.tag_write_log import TagWriteStatus


if TYPE_CHECKING:
    from pathlib import Path
from phaze.services.tag_writer import (
    _write_mp4,
    _write_vorbis,
    execute_tag_write,
    verify_write,
    write_tags,
)


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

    def test_write_tags_skips_none_values(self, mp3_file: Path) -> None:
        """write_tags skips fields with None values."""
        write_tags(str(mp3_file), {"artist": "Test", "title": None})

        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TIT2" not in audio.tags


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

    def test_verify_skips_none_expected(self, mp3_file: Path) -> None:
        """verify_write skips fields where expected is None."""
        write_tags(str(mp3_file), {"artist": "Test"})
        discrepancies = verify_write(str(mp3_file), {"artist": "Test", "title": None})
        assert discrepancies == {}


class TestExecuteTagWrite:
    """Tests for execute_tag_write async function."""

    def _make_file_record(self, state: str = FileState.EXECUTED, current_path: str = "/mock/dest/test.mp3") -> MagicMock:
        """Create a mock FileRecord."""
        fr = MagicMock()
        fr.state = state
        fr.current_path = current_path
        fr.id = MagicMock()
        return fr

    @pytest.mark.asyncio
    async def test_rejects_non_executed_file(self) -> None:
        """execute_tag_write raises ValueError for non-EXECUTED files."""
        fr = self._make_file_record(state=FileState.APPROVED)
        session = AsyncMock()
        with pytest.raises(ValueError, match="executed"):
            await execute_tag_write(session, fr, {"artist": "Test"}, "tracklist")

    @pytest.mark.asyncio
    async def test_creates_tag_write_log_on_success(self, mp3_file: Path) -> None:
        """execute_tag_write creates a TagWriteLog entry on successful write."""
        fr = self._make_file_record(current_path=str(mp3_file))
        session = AsyncMock()

        log_entry = await execute_tag_write(session, fr, {"artist": "New Artist"}, "tracklist")

        assert log_entry.status == TagWriteStatus.COMPLETED
        assert log_entry.source == "tracklist"
        assert log_entry.after_tags == {"artist": "New Artist"}
        assert isinstance(log_entry.before_tags, dict)
        session.add.assert_called_once()
        session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_creates_failed_log_on_error(self, tmp_path: Path) -> None:
        """execute_tag_write creates a FAILED log entry when write errors."""
        bad_path = tmp_path / "nonexistent.mp3"
        fr = self._make_file_record(current_path=str(bad_path))
        session = AsyncMock()

        log_entry = await execute_tag_write(session, fr, {"artist": "Test"}, "manual_edit")

        assert log_entry.status == TagWriteStatus.FAILED
        assert log_entry.error_message is not None
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_current_path_not_original(self) -> None:
        """execute_tag_write uses file_record.current_path."""
        fr = self._make_file_record(current_path="/dest/music.mp3")
        fr.state = FileState.EXECUTED
        session = AsyncMock()

        with (
            patch("phaze.services.tag_writer.extract_tags") as mock_extract,
            patch("phaze.services.tag_writer.write_tags") as mock_write,
            patch("phaze.services.tag_writer.verify_write", return_value={}),
        ):
            mock_extract.return_value = MagicMock(artist=None, title=None, album=None, year=None, genre=None, track_number=None)
            await execute_tag_write(session, fr, {"artist": "Test"}, "tracklist")
            mock_write.assert_called_once_with("/dest/music.mp3", {"artist": "Test"})

    @pytest.mark.asyncio
    async def test_discrepancy_status(self, mp3_file: Path) -> None:
        """execute_tag_write returns DISCREPANCY status when verify finds mismatches."""
        fr = self._make_file_record(current_path=str(mp3_file))
        session = AsyncMock()

        with patch("phaze.services.tag_writer.verify_write", return_value={"artist": {"expected": "A", "actual": "B"}}):
            log_entry = await execute_tag_write(session, fr, {"artist": "A"}, "tracklist")
            assert log_entry.status == TagWriteStatus.DISCREPANCY
            assert log_entry.discrepancies == {"artist": {"expected": "A", "actual": "B"}}
