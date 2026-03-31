"""Tag extraction service using mutagen for audio metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4


logger = logging.getLogger(__name__)


@dataclass
class ExtractedTags:
    """Normalized tag data extracted from an audio/video file."""

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    duration: float | None = None
    bitrate: int | None = None
    raw_tags: dict[str, Any] = field(default_factory=dict)


# Tag key mappings for each format family
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


def _first_str(val: Any) -> str | None:
    """Extract the first string from a tag value.

    Handles lists, ID3 text frames, and plain strings.
    """
    if val is None:
        return None
    if isinstance(val, list):
        return str(val[0]) if val else None
    return str(val)


def _parse_year(val: str | None) -> int | None:
    """Parse a year integer from a date string.

    Handles formats like "2024", "2024-03-15", and invalid values.
    """
    if val is None:
        return None
    text = str(val).strip()
    if not text:
        return None
    # Take first 4 characters for "2024-03-15" format
    year_str = text[:4]
    try:
        year = int(year_str)
    except ValueError:
        return None
    # Sanity check: reasonable year range
    if 1000 <= year <= 9999:
        return year
    return None


def _parse_track(val: Any) -> int | None:
    """Parse a track number from various formats.

    Handles: "3", "3/12", (3, 12), [(3, 12)], and invalid values.
    """
    if val is None:
        return None

    # Handle list of tuples (MP4 trkn format)
    if isinstance(val, list):
        if not val:
            return None
        first = val[0]
        if isinstance(first, tuple) and len(first) >= 1:
            try:
                return int(first[0]) if first[0] else None
            except (ValueError, TypeError):
                return None
        val = first

    # Handle tuple directly
    if isinstance(val, tuple) and len(val) >= 1:
        try:
            return int(val[0]) if val[0] else None
        except (ValueError, TypeError):
            return None

    # Handle string: "3" or "3/12"
    text = str(val).strip()
    if "/" in text:
        text = text.split("/")[0].strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _serialize_tags(tags: Any) -> dict[str, Any]:
    """Serialize all tags to a JSON-safe dict.

    Skips binary values (cover art / APIC frames) and uses str() fallback
    for non-standard types.
    """
    if tags is None:
        return {}

    result: dict[str, Any] = {}
    try:
        items: list[tuple[str, Any]] = list(tags.items()) if hasattr(tags, "items") else []
    except Exception:
        return {}

    for key, val in items:
        str_key = str(key)
        # Skip APIC (cover art) frames entirely
        if str_key.startswith("APIC"):
            continue
        try:
            if isinstance(val, bytes):
                continue
            if isinstance(val, list):
                serialized = []
                for item in val:
                    if isinstance(item, bytes):
                        continue
                    serialized.append(str(item))
                if serialized:
                    result[str_key] = serialized
            else:
                result[str_key] = str(val)
        except Exception:
            logger.debug("Failed to serialize tag %s", str_key)
            continue

    return result


def extract_tags(file_path: str) -> ExtractedTags:
    """Extract audio tags from a file using mutagen.

    Returns an ExtractedTags dataclass with normalized fields and raw tag dump.
    On any error or missing tags, returns ExtractedTags with all None fields
    and an empty raw_tags dict.
    """
    try:
        audio = mutagen.File(file_path)
    except Exception:
        logger.debug("Failed to open file with mutagen: %s", file_path)
        return ExtractedTags()

    if audio is None:
        return ExtractedTags()

    # Extract duration and bitrate from audio.info
    duration: float | None = None
    bitrate: int | None = None
    info = getattr(audio, "info", None)
    if info is not None:
        length = getattr(info, "length", None)
        if length is not None and length > 0:
            duration = float(length)
        br = getattr(info, "bitrate", None)
        if br is not None:
            bitrate = int(br)

    # Extract raw tags
    raw_tags = _serialize_tags(audio.tags)

    # Extract normalized fields based on tag type
    fields: dict[str, Any] = {}

    if audio.tags is None:
        # File has no tags
        return ExtractedTags(duration=duration, bitrate=bitrate, raw_tags=raw_tags)

    if isinstance(audio.tags, ID3):
        # ID3-tagged files (MP3, AIFF, etc.)
        for id3_key, field_name in _ID3_MAP.items():
            frame = audio.tags.get(id3_key)
            if frame is not None:
                fields[field_name] = _first_str(getattr(frame, "text", [frame]))
    elif isinstance(audio, MP4):
        # MP4/M4A atoms
        for mp4_key, field_name in _MP4_MAP.items():
            val = audio.tags.get(mp4_key)
            if val is not None:
                if field_name == "track_number":
                    fields[field_name] = val  # Keep as-is for _parse_track
                else:
                    fields[field_name] = _first_str(val)
    else:
        # Vorbis-style comments (OGG, FLAC, OPUS)
        for vorbis_key, field_name in _VORBIS_MAP.items():
            val = audio.tags.get(vorbis_key)
            if val is not None:
                fields[field_name] = _first_str(val)

    return ExtractedTags(
        artist=fields.get("artist"),
        title=fields.get("title"),
        album=fields.get("album"),
        year=_parse_year(fields.get("year")),
        genre=fields.get("genre"),
        track_number=_parse_track(fields.get("track_number")),
        duration=duration,
        bitrate=bitrate,
        raw_tags=raw_tags,
    )
