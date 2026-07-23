"""Tests for the tag extraction service (src/phaze/services/metadata.py)."""

from unittest.mock import MagicMock, patch

import pytest

from phaze.services.metadata import (
    ExtractedTags,
    TagReadError,
    _first_str,
    _parse_track,
    _parse_year,
    _sanitize_pg_text,
    _serialize_tags,
    extract_tags,
)


def _has_surrogate(s: str) -> bool:
    """Return True if any char in the string is a Unicode surrogate (U+D800-U+DFFF)."""
    return any("\ud800" <= ch <= "\udfff" for ch in s)


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

    def test_items_raising_returns_empty_dict(self):
        """If tags.items() raises, serialization degrades to {} rather than propagating."""
        tags = MagicMock()
        tags.items.side_effect = RuntimeError("mapping blew up")
        assert _serialize_tags(tags) == {}

    def test_skips_binary_items_inside_list_value(self):
        """A list value with mixed bytes/str keeps only the string items."""
        tags = MagicMock()
        tags.items.return_value = [("mixed", [b"\x00cover", "keep-me", b"\xff"])]
        result = _serialize_tags(tags)
        assert result["mixed"] == ["keep-me"]

    def test_value_str_raising_is_skipped(self):
        """A value whose str() raises is dropped, not fatal to the whole dict."""

        class _Explosive:
            def __str__(self) -> str:
                raise ValueError("cannot stringify")

        tags = MagicMock()
        tags.items.return_value = [("bad", _Explosive()), ("good", "ok")]
        result = _serialize_tags(tags)
        assert "bad" not in result
        assert result["good"] == "ok"


class TestParseYearRange:
    """Boundary tests for _parse_year's sanity range (1000-9999)."""

    def test_year_below_range_returns_none(self):
        assert _parse_year("999") is None

    def test_year_at_lower_bound_is_kept(self):
        assert _parse_year("1000") == 1000


class TestParseTrackErrorBranches:
    """Non-integer track values across the list/tuple shapes return None."""

    def test_list_of_tuple_with_non_int_first_returns_none(self):
        assert _parse_track([("x", 12)]) is None

    def test_list_first_non_tuple_falls_through_to_string(self):
        # first element is a bare string -> reassigned to val -> parsed as "5"
        assert _parse_track(["5"]) == 5

    def test_bare_tuple_with_non_int_first_returns_none(self):
        assert _parse_track(("x", 12)) is None


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


class TestExtractTagsReadFailure:
    """phaze-todn: an I/O failure must PROPAGATE, never masquerade as an empty extraction.

    Swallowing OSError here made the metadata stage upsert an all-None row and report
    success for a file it never read (moved/deleted file, media-mount hiccup) -- the
    task's terminal-failure/retry machinery exists for exactly these errors and was
    unreachable.
    """

    @patch("phaze.services.metadata.mutagen.File")
    def test_nonexistent_file_raises(self, mock_file):
        mock_file.side_effect = FileNotFoundError("No such file")

        with pytest.raises(FileNotFoundError, match="No such file"):
            extract_tags("/nonexistent/file.mp3")

    @patch("phaze.services.metadata.mutagen.File")
    def test_transient_os_error_raises(self, mock_file):
        mock_file.side_effect = OSError("Input/output error")

        with pytest.raises(OSError, match="Input/output error"):
            extract_tags("/mnt/media/track.mp3")

    @patch("phaze.services.metadata.mutagen.File")
    def test_mutagen_wrapped_io_error_raises_the_underlying_os_error(self, mock_file):
        """mutagen wraps open failures in MutagenError (not an OSError subclass); the
        underlying OSError must be unwrapped and re-raised, not treated as a parse error."""
        import mutagen

        original = FileNotFoundError(2, "No such file or directory")
        wrapped = mutagen.MutagenError(original)
        wrapped.__cause__ = original
        mock_file.side_effect = wrapped

        with pytest.raises(FileNotFoundError):
            extract_tags("/gone/track.mp3")

    @patch("phaze.services.metadata.mutagen.File")
    def test_strict_mode_wraps_os_error_in_tag_read_error(self, mock_file):
        """Strict mode keeps the phaze-vq3g contract: every open failure becomes TagReadError."""
        mock_file.side_effect = OSError("Input/output error")

        with pytest.raises(TagReadError, match="Input/output error"):
            extract_tags("/mnt/media/track.mp3", strict=True)

    @patch("phaze.services.metadata.mutagen.File")
    def test_parse_error_still_returns_empty(self, mock_file):
        """A readable file whose tags mutagen cannot parse stays a successful empty extraction."""
        mock_file.side_effect = ValueError("can't sync to MPEG frame")

        result = extract_tags("/music/corrupt_header.mp3")

        assert isinstance(result, ExtractedTags)
        assert result.artist is None
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


class TestStripsNulBytes:
    """Regression tests: NUL bytes (U+0000) must never leave the agent.

    PostgreSQL text/jsonb columns reject U+0000, so messy archive tags
    containing NUL bytes must be stripped from both the normalized string
    fields (via _first_str) and raw_tags (via _serialize_tags).
    """

    def test_first_str_strips_nul_from_scalar(self):
        assert _first_str("a\x00b") == "ab"

    def test_first_str_strips_nul_from_list(self):
        assert _first_str(["x\x00y"]) == "xy"

    def test_first_str_none_still_returns_none(self):
        assert _first_str(None) is None

    def test_first_str_empty_list_still_returns_none(self):
        assert _first_str([]) is None

    def test_serialize_tags_strips_nul_everywhere(self):
        tags = MagicMock()
        tags.items.return_value = [
            ("TIT2", "Song\x00Title"),
            ("artist", ["Art\x00ist"]),
            ("KE\x00Y", "val"),
        ]

        result = _serialize_tags(tags)

        for key, value in result.items():
            assert "\x00" not in key
            if isinstance(value, list):
                for item in value:
                    assert "\x00" not in item
            else:
                assert "\x00" not in value

        assert result["TIT2"] == "SongTitle"
        assert result["artist"] == ["Artist"]
        assert result["KEY"] == "val"

    @patch("phaze.services.metadata.mutagen.File")
    def test_extract_tags_strips_nul_from_id3(self, mock_file):
        from mutagen.id3 import ID3

        mock_audio = MagicMock()
        mock_tags = MagicMock(spec=ID3)

        mock_tpe1 = MagicMock()
        mock_tpe1.text = ["Test\x00Artist"]

        def id3_get(key):
            if key == "TPE1":
                return mock_tpe1
            return None

        mock_tags.get = id3_get
        mock_tags.items.return_value = [("TPE1", mock_tpe1)]

        mock_audio.tags = mock_tags
        mock_audio.info = MagicMock()
        mock_audio.info.length = 100.0
        mock_audio.info.bitrate = 128000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.mp3")

        assert result.artist == "TestArtist"
        assert "\x00" not in result.artist
        for value in result.raw_tags.values():
            if isinstance(value, list):
                for item in value:
                    assert "\x00" not in item
            else:
                assert "\x00" not in value


class TestFirstStrRepairsMojibake:
    """phaze-x4ux: _first_str is the metadata-extraction ingest boundary for repair_mojibake.

    artist/title/album/genre are the ONE place a mis-decoded tag gets persisted, so repairing
    here means every downstream reader (search, tracklist matching, rename proposals) sees clean
    text -- see services/metadata.py::_first_str's docstring.
    """

    def test_repairs_double_encoded_scalar(self):
        assert _first_str("Sven VÃƒÂ¤th") == "Sven Väth"

    def test_repairs_double_encoded_list(self):
        assert _first_str(["Sven VÃƒÂ¤th"]) == "Sven Väth"

    def test_no_op_on_already_clean_text(self):
        assert _first_str("Sven Väth") == "Sven Väth"
        assert _first_str("Björk") == "Björk"

    def test_no_op_on_pure_ascii(self):
        assert _first_str("Carl Cox") == "Carl Cox"

    @patch("phaze.services.metadata.mutagen.File")
    def test_extract_tags_repairs_mojibake_artist(self, mock_file):
        """End-to-end through extract_tags (ID3 TPE1), not just the _first_str unit."""
        from mutagen.id3 import ID3

        mock_audio = MagicMock()
        mock_tags = MagicMock(spec=ID3)

        mock_tpe1 = MagicMock()
        mock_tpe1.text = ["Sven VÃƒÂ¤th"]

        def id3_get(key):
            if key == "TPE1":
                return mock_tpe1
            return None

        mock_tags.get = id3_get
        mock_tags.items.return_value = [("TPE1", mock_tpe1)]

        mock_audio.tags = mock_tags
        mock_audio.info = MagicMock()
        mock_audio.info.length = 100.0
        mock_audio.info.bitrate = 128000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.mp3")

        assert result.artist == "Sven Väth"


class TestSanitizePgText:
    """Direct tests for _sanitize_pg_text: strip NUL + lone surrogates, preserve the rest."""

    def test_strips_nul(self):
        assert _sanitize_pg_text("a\x00b") == "ab"

    def test_strips_lone_high_surrogate(self):
        result = _sanitize_pg_text("a\ud83db")
        assert result == "ab"
        assert not _has_surrogate(result)

    def test_strips_lone_low_surrogate(self):
        result = _sanitize_pg_text("a\udc00b")
        assert result == "ab"
        assert not _has_surrogate(result)

    def test_strips_entire_surrogate_range(self):
        for cp in (0xD800, 0xDABC, 0xDC00, 0xDFFF):
            assert _sanitize_pg_text(chr(cp)) == ""

    def test_preserves_c0_control_char(self):
        # U+0007 BELL is a legal C0 control char in a UTF8 text/jsonb column -- must survive.
        assert _sanitize_pg_text("a\x07b") == "a\x07b"

    def test_preserves_noncharacter(self):
        # U+FFFE is a Unicode noncharacter but is valid in a UTF8 PostgreSQL column.
        assert _sanitize_pg_text("a￾b") == "a￾b"

    def test_preserves_valid_astral_char(self):
        # A real astral character (single code point, not a surrogate pair) must survive.
        assert _sanitize_pg_text("emoji \U0001f600 ok") == "emoji \U0001f600 ok"


class TestStripsLoneSurrogates:
    """Regression tests: lone Unicode surrogates (U+D800-U+DFFF) must never leave the agent.

    PostgreSQL jsonb rejects lone surrogates and asyncpg cannot encode them to UTF-8,
    so they must be stripped from normalized fields and raw_tags alike.
    """

    def test_first_str_strips_lone_high_surrogate(self):
        result = _first_str("hi\ud83dthere")
        assert result is not None
        assert not _has_surrogate(result)
        assert result == "hithere"

    def test_first_str_strips_lone_low_surrogate_from_list(self):
        result = _first_str(["lo\udc00w"])
        assert result is not None
        assert not _has_surrogate(result)
        assert result == "low"

    def test_serialize_tags_strips_surrogates_everywhere(self):
        tags = MagicMock()
        tags.items.return_value = [
            ("TIT2", "Song\ud83dTitle"),
            ("artist", ["Art\udc00ist"]),
            ("KE\ud800Y", "val"),
        ]

        result = _serialize_tags(tags)

        for key, value in result.items():
            assert not _has_surrogate(key)
            assert "\x00" not in key
            if isinstance(value, list):
                for item in value:
                    assert not _has_surrogate(item)
                    assert "\x00" not in item
            else:
                assert not _has_surrogate(value)
                assert "\x00" not in value

        assert result["TIT2"] == "SongTitle"
        assert result["artist"] == ["Artist"]
        assert result["KEY"] == "val"

    @patch("phaze.services.metadata.mutagen.File")
    def test_extract_tags_strips_surrogates_from_id3(self, mock_file):
        from mutagen.id3 import ID3

        mock_audio = MagicMock()
        mock_tags = MagicMock(spec=ID3)

        mock_tpe1 = MagicMock()
        mock_tpe1.text = ["Test\ud83dArtist"]
        mock_tit2 = MagicMock()
        mock_tit2.text = ["Ti\udc00tle"]

        def id3_get(key):
            return {"TPE1": mock_tpe1, "TIT2": mock_tit2}.get(key)

        mock_tags.get = id3_get
        mock_tags.items.return_value = [("TPE1", mock_tpe1), ("TIT2", mock_tit2)]

        mock_audio.tags = mock_tags
        mock_audio.info = MagicMock()
        mock_audio.info.length = 100.0
        mock_audio.info.bitrate = 128000

        mock_file.return_value = mock_audio

        result = extract_tags("/fake/path.mp3")

        assert result.artist == "TestArtist"
        assert result.title == "Title"
        assert result.artist is not None and not _has_surrogate(result.artist)
        assert result.title is not None and not _has_surrogate(result.title)
        for value in result.raw_tags.values():
            items = value if isinstance(value, list) else [value]
            for item in items:
                assert not _has_surrogate(item)
                assert "\x00" not in item
