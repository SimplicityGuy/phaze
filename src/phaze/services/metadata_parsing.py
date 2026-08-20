"""Pure format-specific normalization for opened mutagen audio/tag objects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from phaze.services.pg_text import sanitize_pg_text
from phaze.services.text_repair import repair_mojibake


_VORBIS_MAP: dict[str, str] = {
    "artist": "artist",
    "title": "title",
    "album": "album",
    "date": "year",
    "genre": "genre",
    "tracknumber": "track_number",
}

_ID3_MAP: dict[str, str] = {
    "TPE1": "artist",
    "TIT2": "title",
    "TALB": "album",
    "TDRC": "year",
    "TCON": "genre",
    "TRCK": "track_number",
}

_MP4_MAP: dict[str, str] = {
    "\xa9ART": "artist",
    "\xa9nam": "title",
    "\xa9alb": "album",
    "\xa9day": "year",
    "\xa9gen": "genre",
    "trkn": "track_number",
}


@dataclass(frozen=True)
class ParsedTagValues:
    """Normalized fields plus the parallel raw values needed for lossless undo."""

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    raw_year: str | None = None
    raw_track_number: str | None = None
    raw_genre: str | list[str] | None = None


def _first_str(val: Any) -> str | None:
    """Extract, sanitize, and repair the first string from a tag value."""
    if val is None:
        return None
    if isinstance(val, list):
        return repair_mojibake(sanitize_pg_text(str(val[0]))) if val else None
    return repair_mojibake(sanitize_pg_text(str(val)))


def _parse_year(val: str | None) -> int | None:
    """Parse a four-digit year from a year or full-date string."""
    if val is None:
        return None
    text = str(val).strip()
    if not text:
        return None
    try:
        year = int(text[:4])
    except ValueError:
        return None
    return year if 1000 <= year <= 9999 else None


def _bounded_track(number: int) -> int | None:
    """Keep a parsed track number inside the wire contract's 0..9999 domain."""
    return number if 0 <= number <= 9999 else None


def _parse_track(val: Any) -> int | None:
    """Parse ``N``, ``N/total``, tuple, and list-of-tuple track shapes."""
    value = _first_track_value(val)
    if value is None:
        return None
    if isinstance(value, tuple):
        return _parse_track_tuple(value)
    return _parse_track_text(value)


def _first_track_value(value: Any) -> Any:
    """Unwrap mutagen's list container while preserving all other track shapes."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _parse_track_tuple(value: tuple[Any, ...]) -> int | None:
    """Parse the leading element of an MP4 ``trkn`` tuple."""
    if not value or value[0] is None:
        return None
    try:
        return _bounded_track(int(value[0]))
    except (ValueError, TypeError):
        return None


def _parse_track_text(value: Any) -> int | None:
    """Parse a scalar ``N`` or ``N/total`` track value."""
    text = str(value).strip()
    if "/" in text:
        text = text.split("/")[0].strip()
    try:
        return _bounded_track(int(text)) if text else None
    except ValueError:
        return None


def normalize_year_text(value: Any) -> int | None:
    """Normalize raw year/date text with the extraction rule."""
    return _parse_year(str(value)) if value is not None else None


def normalize_track_number_text(value: Any) -> int | None:
    """Normalize raw track text with the extraction rule."""
    return _parse_track(value)


def _raw_genre_value(val: Any) -> str | list[str] | None:
    """Preserve one genre as text and multiple genres as separate values."""
    if val is None:
        return None
    if isinstance(val, list):
        parts = [sanitize_pg_text(str(item)) for item in val if not isinstance(item, bytes)]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else parts
    return sanitize_pg_text(str(val))


def _raw_track_text(val: Any) -> str | None:
    """Preserve raw ``N`` / ``N/total`` text across every supported track shape."""
    value = _first_track_value(val)
    if value is None:
        return None
    if isinstance(value, tuple):
        return _raw_track_tuple(value)
    text = sanitize_pg_text(str(value)).strip()
    return text or None


def _raw_track_tuple(value: tuple[Any, ...]) -> str | None:
    """Render an MP4 ``trkn`` tuple without losing a valid zero track."""
    if len(value) >= 2 and value[1]:
        return f"{value[0]}/{value[1]}"
    if value and value[0] is not None:
        return str(value[0])
    return None


ValueReader = Callable[[str, Any], Any]


def _id3_value(_field_name: str, frame: Any) -> Any:
    """Read the ID3 frame payload exactly as mutagen exposes it."""
    return getattr(frame, "text", [frame])


def _mp4_value(field_name: str, value: Any) -> Any:
    """Keep ``trkn`` structured; normalize the other MP4 list atoms as text."""
    return value if field_name == "track_number" else _first_str(value)


def _text_value(_field_name: str, value: Any) -> Any:
    """Normalize ID3/Vorbis values through their first text value."""
    return _first_str(value)


def _mapped_sources(tags: Any, mapping: Mapping[str, str], source_reader: ValueReader, field_reader: ValueReader) -> ParsedTagValues:
    """Read mapped fields, retaining raw genre/track sources beside normalized inputs."""
    fields: dict[str, Any] = {}
    raw_sources: dict[str, Any] = {}
    for tag_key, field_name in mapping.items():
        value = tags.get(tag_key)
        if value is None:
            continue
        source = source_reader(field_name, value)
        fields[field_name] = field_reader(field_name, source)
        if field_name in ("genre", "track_number"):
            raw_sources[field_name] = source
    return _normalized_values(fields, raw_sources)


def _normalized_values(fields: Mapping[str, Any], raw_sources: Mapping[str, Any]) -> ParsedTagValues:
    """Build the typed normalized-plus-raw result from format-specific sources."""
    return ParsedTagValues(
        artist=fields.get("artist"),
        title=fields.get("title"),
        album=fields.get("album"),
        year=_parse_year(fields.get("year")),
        genre=fields.get("genre"),
        track_number=_parse_track(fields.get("track_number")),
        raw_year=fields.get("year"),
        raw_track_number=_raw_track_text(raw_sources.get("track_number")),
        raw_genre=_raw_genre_value(raw_sources.get("genre")),
    )


def parse_format_tags(audio: Any, tags: Any) -> ParsedTagValues:
    """Purely normalize opened mutagen objects according to their tag family."""
    if tags is None:
        return ParsedTagValues()
    if isinstance(tags, ID3):
        return _mapped_sources(tags, _ID3_MAP, _id3_value, _text_value)
    if isinstance(audio, MP4):
        return _mapped_sources(tags, _MP4_MAP, lambda _field, value: value, _mp4_value)
    return _mapped_sources(tags, _VORBIS_MAP, lambda _field, value: value, _text_value)
