"""Tests for the tag writer service."""

from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import mutagen
from mutagen.id3 import ID3, TIT2, TPE1
from mutagen.mp3 import MP3
import pytest
import pytest_asyncio

from phaze.models.file import FileState
from phaze.models.tag_write_log import TagWriteStatus
from phaze.services.tag_writer import execute_tag_write, verify_write, write_tags


# ---------------------------------------------------------------------------
# Fixtures: minimal valid audio files
# ---------------------------------------------------------------------------

def _make_mp3(path: Path) -> Path:
    """Create a minimal valid MP3 file with an ID3 header."""
    # Minimal MPEG frame: MPEG1 Layer3 128kbps 44100Hz stereo
    # Sync word + header bytes for a valid frame
    frame_header = b"\xff\xfb\x90\x00"
    # Pad to frame size (417 bytes for 128kbps @ 44100Hz)
    frame_data = frame_header + b"\x00" * 413
    path.write_bytes(frame_data)
    # Add ID3 tags via mutagen
    audio = MP3(str(path))
    audio.add_tags()
    audio.save()
    return path


def _make_ogg(path: Path) -> Path:
    """Create a minimal valid OGG Vorbis file using mutagen test data."""
    import shutil

    try:
        # Use mutagen's bundled test data for a valid OGG file
        test_data_dir = Path(mutagen.__file__).parent.parent / "tests" / "data"
        if (test_data_dir / "empty.ogg").exists():
            shutil.copy(test_data_dir / "empty.ogg", path)
            return path
    except Exception:
        pass

    # Fallback: create a minimal OGG manually using struct
    ogg_page = bytearray()
    ogg_page.extend(b"OggS")  # capture pattern
    ogg_page.extend(b"\x00")  # version
    ogg_page.extend(b"\x02")  # header type (beginning of stream)
    ogg_page.extend(struct.pack("<q", 0))  # granule position
    ogg_page.extend(struct.pack("<I", 1))  # serial number
    ogg_page.extend(struct.pack("<I", 0))  # page sequence
    ogg_page.extend(struct.pack("<I", 0))  # CRC
    ogg_page.extend(b"\x01")  # 1 segment
    ogg_page.extend(b"\x1e")  # segment size = 30

    # Vorbis identification header
    vorbis_id = bytearray()
    vorbis_id.extend(b"\x01vorbis")
    vorbis_id.extend(struct.pack("<I", 0))  # version
    vorbis_id.extend(b"\x01")  # channels
    vorbis_id.extend(struct.pack("<I", 44100))  # sample rate
    vorbis_id.extend(struct.pack("<i", 0))  # bitrate max
    vorbis_id.extend(struct.pack("<i", 128000))  # bitrate nominal
    vorbis_id.extend(struct.pack("<i", 0))  # bitrate min
    vorbis_id.extend(b"\xb8")  # blocksize 0=256, 1=2048

    ogg_page.extend(vorbis_id)
    path.write_bytes(bytes(ogg_page))
    return path


def _make_m4a(path: Path) -> Path:
    """Create a minimal valid M4A file."""
    from mutagen.mp4 import MP4

    # Create minimal ftyp + moov atoms for a valid MP4
    # ftyp box
    ftyp = b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A "
    # minimal moov with mvhd
    mvhd = b"\x00\x00\x00\x6cmvhd" + b"\x00" * 100
    moov = struct.pack(">I", len(mvhd) + 8) + b"moov" + mvhd
    path.write_bytes(ftyp + moov)
    return path


@pytest.fixture
def mp3_file(tmp_path: Path) -> Path:
    """Create a temporary MP3 file."""
    return _make_mp3(tmp_path / "test.mp3")


class TestWriteTags:
    """Tests for write_tags function."""

    def test_write_id3_tags_to_mp3(self, mp3_file: Path) -> None:
        """Write ID3 tags to an MP3 file and read them back."""
        tags = {"artist": "Test Artist", "title": "Test Title", "album": "Test Album", "year": "2024", "genre": "Electronic", "track_number": "3"}
        write_tags(str(mp3_file), tags)

        # Read back
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


class TestWriteTagsVorbis:
    """Tests for write_tags with Vorbis format (OGG)."""

    def test_write_vorbis_tags(self, tmp_path: Path) -> None:
        """Write Vorbis tags to an OGG file and read them back."""
        ogg_path = _make_ogg(tmp_path / "test.ogg")
        audio = mutagen.File(str(ogg_path))
        if audio is None:
            pytest.skip("Cannot create valid OGG test file in this environment")

        tags = {"artist": "Vorbis Artist", "title": "Vorbis Title"}
        write_tags(str(ogg_path), tags)

        audio = mutagen.File(str(ogg_path))
        assert audio is not None
        assert audio["artist"] == ["Vorbis Artist"]
        assert audio["title"] == ["Vorbis Title"]


class TestWriteTagsMP4:
    """Tests for write_tags with MP4/M4A format."""

    def test_write_mp4_tags(self, tmp_path: Path) -> None:
        """Write MP4 tags and verify track number tuple format."""
        m4a_path = _make_m4a(tmp_path / "test.m4a")
        audio = mutagen.File(str(m4a_path))
        if audio is None:
            pytest.skip("Cannot create valid M4A test file in this environment")

        tags = {"artist": "MP4 Artist", "track_number": "5"}
        write_tags(str(m4a_path), tags)

        audio = mutagen.File(str(m4a_path))
        assert audio is not None
        assert audio["\xa9ART"] == ["MP4 Artist"]
        assert audio["trkn"] == [(5, 0)]


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
        # Verify against different expected
        discrepancies = verify_write(str(mp3_file), {"artist": "Expected Artist"})
        assert "artist" in discrepancies
        assert discrepancies["artist"]["expected"] == "Expected Artist"
        assert discrepancies["artist"]["actual"] == "Actual Artist"


class TestExecuteTagWrite:
    """Tests for execute_tag_write async function."""

    def _make_file_record(self, state: str = FileState.EXECUTED, current_path: str = "/tmp/test.mp3") -> MagicMock:
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

        with patch("phaze.services.tag_writer.extract_tags") as mock_extract, \
             patch("phaze.services.tag_writer.write_tags") as mock_write, \
             patch("phaze.services.tag_writer.verify_write", return_value={}):
            mock_extract.return_value = MagicMock(
                artist=None, title=None, album=None, year=None, genre=None, track_number=None
            )
            await execute_tag_write(session, fr, {"artist": "Test"}, "tracklist")
            mock_write.assert_called_once_with("/dest/music.mp3", {"artist": "Test"})
