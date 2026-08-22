"""Table-driven characterization of pure format-specific metadata parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from mutagen._vorbis import VCommentDict
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
import pytest

from phaze.services.metadata import ExtractedTags, TagReadError, extract_tags
from phaze.services.metadata_parsing import (
    ParsedTagValues,
    _raw_track_text,
    normalize_track_number_text,
    normalize_year_text,
    parse_format_tags,
)


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


def _vorbis_case() -> tuple[MagicMock, VCommentDict]:
    """phaze-wt9vw: a REAL ``VCommentDict``, unlike the other two cases' mapping stand-ins.

    The Vorbis arm of ``parse_format_tags`` used to be the dispatch's unguarded default -- every
    container that was not ID3 or MP4 landed there, so any mapping-shaped mock was read as Vorbis.
    That fallback is what made the ``.wma`` defect self-confirming, and it is gone: the format is
    now resolved by ``tag_formats.resolve_tag_format``, which recognises Vorbis by the genuine
    ``VCommentDict`` base shared by ``VCFLACDict``, ``OggVCommentDict`` and ``OggOpusVComment``.
    A fixture that is not really a Vorbis comment container is therefore no longer read as one.
    """
    tags = VCommentDict()
    tags["artist"] = ["Vorbis Artist"]
    tags["title"] = ["Vorbis Title"]
    tags["album"] = ["Vorbis Album"]
    tags["date"] = ["2022"]
    tags["genre"] = ["Rock", "Alternative"]
    tags["tracknumber"] = ["7/14"]
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


def test_raw_track_text_list_of_none_is_none_not_the_string_none() -> None:
    """``[None]`` is unreachable from a real mutagen tag, but pin the deliberate divergence:

    it now returns ``None`` (restoring the phaze-2zl7 "raw is None iff normalized is None"
    contract) rather than the pre-refactor literal string ``"None"``.
    """
    assert _raw_track_text([None]) is None
