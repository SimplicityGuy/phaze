"""Tests for the tag extraction service (src/phaze/services/metadata.py)."""

from unittest.mock import MagicMock, patch

from phaze.services.metadata import (
    ExtractedTags,
    _parse_track,
    _parse_year,
    _serialize_tags,
    extract_tags,
)


class TestParseYear:
    """Tests for _parse_year helper."""

    def test_plain_year(self):
        assert _parse_year("2024") == 2024

    def test_date_with_dashes(self):
        assert _parse_year("2024-03-15") == 2024

    def test_none_returns_none(self):
        assert _parse_year(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_year("") is None

    def test_invalid_returns_none(self):
        assert _parse_year("abcd") is None

    def test_whitespace_stripped(self):
        assert _parse_year("  2024  ") == 2024


class TestParseTrack:
    """Tests for _parse_track helper."""

    def test_plain_number_string(self):
        assert _parse_track("3") == 3

    def test_slash_format(self):
        assert _parse_track("3/12") == 3

    def test_tuple_format(self):
        assert _parse_track((3, 12)) == 3

    def test_list_of_tuples(self):
        assert _parse_track([(3, 12)]) == 3

    def test_none_returns_none(self):
        assert _parse_track(None) is None

    def test_empty_list_returns_none(self):
        assert _parse_track([]) is None

    def test_invalid_returns_none(self):
        assert _parse_track("abc") is None


class TestSerializeTags:
    """Tests for _serialize_tags helper."""

    def test_none_tags_returns_empty_dict(self):
        assert _serialize_tags(None) == {}

    def test_serializes_string_values(self):
        tags = MagicMock()
        tags.items.return_value = [("TIT2", "Song Title")]
        result = _serialize_tags(tags)
        assert result["TIT2"] == "Song Title"

    def test_skips_binary_values(self):
        tags = MagicMock()
        tags.items.return_value = [("data", b"\x00\x01\x02")]
        result = _serialize_tags(tags)
        assert "data" not in result

    def test_skips_apic_frames(self):
        tags = MagicMock()
        tags.items.return_value = [("APIC:cover", b"\xff\xd8\xff\xe0"), ("TIT2", "Title")]
        result = _serialize_tags(tags)
        assert "APIC:cover" not in result
        assert "TIT2" in result

    def test_serializes_list_values(self):
        tags = MagicMock()
        tags.items.return_value = [("artist", ["Artist Name"])]
        result = _serialize_tags(tags)
        assert result["artist"] == ["Artist Name"]


class TestExtractTagsID3:
    """Tests for extract_tags with ID3-tagged (MP3) files."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_extracts_id3_tags(self, mock_file):
        from mutagen.id3 import ID3

        mock_audio = MagicMock()
        mock_tags = MagicMock(spec=ID3)

        # Create mock ID3 frames
        mock_tpe1 = MagicMock()
        mock_tpe1.text = ["Test Artist"]
        mock_tit2 = MagicMock()
        mock_tit2.text = ["Test Title"]
        mock_talb = MagicMock()
        mock_talb.text = ["Test Album"]
        mock_tdrc = MagicMock()
        mock_tdrc.text = ["2024"]
        mock_tcon = MagicMock()
        mock_tcon.text = ["Electronic"]
        mock_trck = MagicMock()
        mock_trck.text = ["3/12"]

        def id3_get(key):
            mapping = {
                "TPE1": mock_tpe1,
                "TIT2": mock_tit2,
                "TALB": mock_talb,
                "TDRC": mock_tdrc,
                "TCON": mock_tcon,
                "TRCK": mock_trck,
            }
            return mapping.get(key)

        mock_tags.get = id3_get
        mock_tags.items.return_value = [
            ("TPE1", mock_tpe1),
            ("TIT2", mock_tit2),
            ("TALB", mock_talb),
            ("TDRC", mock_tdrc),
            ("TCON", mock_tcon),
            ("TRCK", mock_trck),
        ]

        mock_audio.tags = mock_tags
        mock_audio.info = MagicMock()
        mock_audio.info.length = 240.5
        mock_audio.info.bitrate = 320000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.mp3")

        assert isinstance(result, ExtractedTags)
        assert result.artist == "Test Artist"
        assert result.title == "Test Title"
        assert result.album == "Test Album"
        assert result.year == 2024
        assert result.genre == "Electronic"
        assert result.track_number == 3
        assert result.duration == 240.5
        assert result.bitrate == 320000


class TestExtractTagsVorbis:
    """Tests for extract_tags with Vorbis-tagged (OGG/FLAC) files."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_extracts_vorbis_tags(self, mock_file):
        mock_audio = MagicMock()
        # Vorbis tags are NOT ID3 and NOT MP4
        mock_tags = MagicMock()
        # Remove ID3 spec so isinstance check fails
        mock_tags.__class__ = type("VorbisComment", (), {})

        def vorbis_get(key):
            mapping = {
                "artist": ["Vorbis Artist"],
                "title": ["Vorbis Title"],
                "album": ["Vorbis Album"],
                "date": ["2023"],
                "genre": ["Rock"],
                "tracknumber": ["5/10"],
            }
            return mapping.get(key)

        mock_tags.get = vorbis_get
        mock_tags.items.return_value = [
            ("artist", ["Vorbis Artist"]),
            ("title", ["Vorbis Title"]),
            ("album", ["Vorbis Album"]),
            ("date", ["2023"]),
            ("genre", ["Rock"]),
            ("tracknumber", ["5/10"]),
        ]

        mock_audio.tags = mock_tags
        mock_audio.__class__ = type("OggVorbis", (), {})
        mock_audio.info = MagicMock()
        mock_audio.info.length = 180.0
        mock_audio.info.bitrate = 192000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.ogg")

        assert result.artist == "Vorbis Artist"
        assert result.title == "Vorbis Title"
        assert result.album == "Vorbis Album"
        assert result.year == 2023
        assert result.genre == "Rock"
        assert result.track_number == 5
        assert result.duration == 180.0
        assert result.bitrate == 192000


class TestExtractTagsMP4:
    """Tests for extract_tags with MP4-tagged (M4A) files."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_extracts_mp4_tags(self, mock_file):
        from mutagen.mp4 import MP4

        mock_audio = MagicMock(spec=MP4)
        mock_tags = MagicMock()

        def mp4_get(key):
            mapping = {
                "\xa9ART": ["MP4 Artist"],
                "\xa9nam": ["MP4 Title"],
                "\xa9alb": ["MP4 Album"],
                "\xa9day": ["2022"],
                "\xa9gen": ["Pop"],
                "trkn": [(7, 14)],
            }
            return mapping.get(key)

        mock_tags.get = mp4_get
        mock_tags.items.return_value = [
            ("\xa9ART", ["MP4 Artist"]),
            ("\xa9nam", ["MP4 Title"]),
            ("\xa9alb", ["MP4 Album"]),
            ("\xa9day", ["2022"]),
            ("\xa9gen", ["Pop"]),
            ("trkn", [(7, 14)]),
        ]

        mock_audio.tags = mock_tags
        mock_audio.info = MagicMock()
        mock_audio.info.length = 300.0
        mock_audio.info.bitrate = 256000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.m4a")

        assert result.artist == "MP4 Artist"
        assert result.title == "MP4 Title"
        assert result.album == "MP4 Album"
        assert result.year == 2022
        assert result.genre == "Pop"
        assert result.track_number == 7
        assert result.duration == 300.0
        assert result.bitrate == 256000


class TestExtractTagsNoTags:
    """Tests for extract_tags with files that have no tags."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_no_tags_returns_empty_extracted_tags(self, mock_file):
        mock_audio = MagicMock()
        mock_audio.tags = None
        mock_audio.info = MagicMock()
        mock_audio.info.length = 120.0
        mock_audio.info.bitrate = None

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.wav")

        assert result.artist is None
        assert result.title is None
        assert result.album is None
        assert result.year is None
        assert result.genre is None
        assert result.track_number is None
        assert result.duration == 120.0
        assert result.bitrate is None
        assert result.raw_tags == {}


class TestExtractTagsNonexistent:
    """Tests for extract_tags on nonexistent files."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_nonexistent_file_returns_empty(self, mock_file):
        mock_file.side_effect = FileNotFoundError("No such file")

        result = extract_tags("/nonexistent/file.mp3")

        assert isinstance(result, ExtractedTags)
        assert result.artist is None
        assert result.title is None
        assert result.raw_tags == {}


class TestExtractTagsMutagenReturnsNone:
    """Tests for extract_tags when mutagen returns None."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_mutagen_returns_none(self, mock_file):
        mock_file.return_value = None

        result = extract_tags("/fake/unknown_format.xyz")

        assert isinstance(result, ExtractedTags)
        assert result.artist is None
        assert result.raw_tags == {}


class TestExtractTagsBinaryCoverArt:
    """Tests for raw_tags excluding binary cover art."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_binary_cover_art_excluded_from_raw_tags(self, mock_file):
        from mutagen.id3 import ID3

        mock_audio = MagicMock()
        mock_tags = MagicMock(spec=ID3)

        mock_tit2 = MagicMock()
        mock_tit2.text = ["Title"]

        def id3_get(key):
            if key == "TIT2":
                return mock_tit2
            return None

        mock_tags.get = id3_get
        mock_tags.items.return_value = [
            ("TIT2", mock_tit2),
            ("APIC:", b"\xff\xd8\xff\xe0cover_art_bytes"),
        ]

        mock_audio.tags = mock_tags
        mock_audio.info = MagicMock()
        mock_audio.info.length = 100.0
        mock_audio.info.bitrate = 128000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.mp3")

        assert "APIC:" not in result.raw_tags
        assert result.title == "Title"


class TestExtractTagsDurationBitrate:
    """Tests for duration and bitrate extraction from audio.info."""

    @patch("phaze.services.metadata.mutagen.File")
    def test_duration_and_bitrate_from_info(self, mock_file):
        mock_audio = MagicMock()
        mock_audio.tags = None
        mock_audio.info = MagicMock()
        mock_audio.info.length = 365.2
        mock_audio.info.bitrate = 320000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.mp3")

        assert result.duration == 365.2
        assert result.bitrate == 320000

    @patch("phaze.services.metadata.mutagen.File")
    def test_no_bitrate_attribute(self, mock_file):
        mock_audio = MagicMock()
        mock_audio.tags = None
        mock_audio.info = MagicMock(spec=[])  # No attributes at all
        # Manually set length but no bitrate
        mock_audio.info.length = None

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.wav")

        assert result.duration is None
        assert result.bitrate is None
