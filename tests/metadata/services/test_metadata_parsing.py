"""Table-driven characterization of pure format-specific metadata parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from mutagen.id3 import ID3
from mutagen.mp4 import MP4
import pytest

from phaze.services.metadata import ExtractedTags, TagReadError, extract_tags
from phaze.services.metadata_parsing import ParsedTagValues, normalize_track_number_text, normalize_year_text, parse_format_tags


if TYPE_CHECKING:
    from collections.abc import Callable


def _mapping_tags(values: dict[str, object]) -> MagicMock:
    tags = MagicMock()
    tags.get.side_effect = values.get
    return tags


def _id3_case() -> tuple[MagicMock, MagicMock]:
    tags = MagicMock(spec=ID3)
    values = {
        "TPE1": MagicMock(text=["ID3 Artist"]),
        "TIT2": MagicMock(text=["ID3 Title"]),
        "TALB": MagicMock(text=["ID3 Album"]),
        "TDRC": MagicMock(text=["2024-03-15"]),
        "TCON": MagicMock(text=["House", "Techno"]),
        "TRCK": MagicMock(text=["3/12"]),
    }
    tags.get.side_effect = values.get
    audio = MagicMock()
    audio.tags = tags
    return audio, tags


def _mp4_case() -> tuple[MagicMock, MagicMock]:
    tags = _mapping_tags(
        {
            "\xa9ART": ["MP4 Artist"],
            "\xa9nam": ["MP4 Title"],
            "\xa9alb": ["MP4 Album"],
            "\xa9day": ["2023-11-09"],
            "\xa9gen": ["Pop"],
            "trkn": [(0, 12)],
        }
    )
    audio = MagicMock(spec=MP4)
    audio.tags = tags
    return audio, tags


def _vorbis_case() -> tuple[MagicMock, MagicMock]:
    tags = _mapping_tags(
        {
            "artist": ["Vorbis Artist"],
            "title": ["Vorbis Title"],
            "album": ["Vorbis Album"],
            "date": ["2022"],
            "genre": ["Rock", "Alternative"],
            "tracknumber": ["7/14"],
        }
    )
    audio = MagicMock()
    audio.__class__ = type("OggVorbis", (), {})
    audio.tags = tags
    return audio, tags


@pytest.mark.parametrize(
    ("build_case", "expected"),
    [
        (
            _id3_case,
            ParsedTagValues(
                artist="ID3 Artist",
                title="ID3 Title",
                album="ID3 Album",
                year=2024,
                genre="House",
                track_number=3,
                raw_year="2024-03-15",
                raw_track_number="3/12",
                raw_genre=["House", "Techno"],
            ),
        ),
        (
            _mp4_case,
            ParsedTagValues(
                artist="MP4 Artist",
                title="MP4 Title",
                album="MP4 Album",
                year=2023,
                genre="Pop",
                track_number=0,
                raw_year="2023-11-09",
                raw_track_number="0/12",
                raw_genre="Pop",
            ),
        ),
        (
            _vorbis_case,
            ParsedTagValues(
                artist="Vorbis Artist",
                title="Vorbis Title",
                album="Vorbis Album",
                year=2022,
                genre="Rock",
                track_number=7,
                raw_year="2022",
                raw_track_number="7/14",
                raw_genre=["Rock", "Alternative"],
            ),
        ),
    ],
    ids=("mp3-id3", "m4a-mp4", "ogg-vorbis"),
)
def test_format_parser_preserves_normalized_and_raw_round_trip(
    build_case: Callable[[], tuple[MagicMock, MagicMock]],
    expected: ParsedTagValues,
) -> None:
    """Every format keeps lossy normalized fields beside exact undo restoration values."""
    audio, tags = build_case()

    result = parse_format_tags(audio, tags)

    assert result == expected
    assert normalize_year_text(result.raw_year) == result.year
    assert normalize_track_number_text(result.raw_track_number) == result.track_number


@pytest.mark.parametrize(("strict", "raises"), [(False, False), (True, True)], ids=("ingest-degrades", "verify-raises"))
def test_parse_failure_strictness_is_an_explicit_policy_split(strict: bool, raises: bool) -> None:
    """Mutating either side of the strict/non-strict split must break the contract."""
    with patch("phaze.services.metadata.mutagen.File", side_effect=ValueError("bad tag payload")):
        if raises:
            with pytest.raises(TagReadError, match="bad tag payload"):
                extract_tags("/synthetic/bad.mp3", strict=strict)
        else:
            assert extract_tags("/synthetic/bad.mp3", strict=strict) == ExtractedTags()


def test_no_tags_is_a_valid_pure_result() -> None:
    """An opened, recognized file with no tag object is not a parse failure."""
    assert parse_format_tags(MagicMock(), None) == ParsedTagValues()
