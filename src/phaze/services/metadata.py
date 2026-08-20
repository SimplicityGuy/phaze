"""Effect and error-policy boundary for audio metadata extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mutagen
import structlog

from phaze.services.metadata_parsing import (
    _first_str,
    _parse_track,
    _parse_year,
    _raw_genre_value,
    _raw_track_text,
    normalize_track_number_text,
    normalize_year_text,
    parse_format_tags,
)
from phaze.services.pg_text import sanitize_pg_text


logger = structlog.get_logger(__name__)

__all__ = [
    "ExtractedTags",
    "TagReadError",
    "_first_str",
    "_parse_track",
    "_parse_year",
    "_raw_genre_value",
    "_raw_track_text",
    "extract_tags",
    "normalize_track_number_text",
    "normalize_year_text",
]


class TagReadError(Exception):
    """A file could not be opened or parsed during strict verify-after-write."""


def _io_cause(exc: BaseException) -> OSError | None:
    """Return the ``OSError`` in an exception's explicit cause chain, if present."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError):
            return current
        current = current.__cause__
    return None


def _read_failure_result(file_path: str, strict: bool, exc: Exception) -> ExtractedTags:
    """Apply strict-vs-ingest and I/O-vs-parse policy to one mutagen open failure."""
    if strict:
        msg = f"failed to read tags from {file_path}: {exc}"
        raise TagReadError(msg) from exc
    io_error = exc if isinstance(exc, OSError) else _io_cause(exc)
    if io_error is not None:
        raise io_error from exc
    logger.debug("Failed to parse tags with mutagen: %s", file_path)
    return ExtractedTags()


@dataclass
class ExtractedTags:
    """Normalized metadata plus raw values retained for faithful undo restoration."""

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: int | None = None
    genre: str | None = None
    track_number: int | None = None
    duration: float | None = None
    bitrate: int | None = None
    raw_tags: dict[str, Any] = field(default_factory=dict)
    raw_year: str | None = None
    raw_track_number: str | None = None
    raw_genre: str | list[str] | None = None


# Compatibility alias: callers/tests historically imported the sanitizer through this module.
_sanitize_pg_text = sanitize_pg_text


def _serialize_tag_value(value: Any) -> str | list[str] | None:
    """Serialize one tag value, filtering binary payloads without nested control flow."""
    if isinstance(value, bytes):
        return None
    if isinstance(value, list):
        serialized = [_sanitize_pg_text(str(item)) for item in value if not isinstance(item, bytes)]
        return serialized or None
    return _sanitize_pg_text(str(value))


def _serialize_tags(tags: Any) -> dict[str, Any]:
    """Best-effort JSON-safe raw tag dump, excluding artwork and binary values."""
    if tags is None:
        return {}
    try:
        items: list[tuple[str, Any]] = list(tags.items()) if hasattr(tags, "items") else []
    except Exception:
        return {}

    result: dict[str, Any] = {}
    for key, value in items:
        str_key = _sanitize_pg_text(str(key))
        if str_key.startswith("APIC"):
            continue
        try:
            serialized = _serialize_tag_value(value)
            if serialized is not None:
                result[str_key] = serialized
        except Exception:
            logger.debug("Failed to serialize tag %s", str_key)
    return result


def extract_tags(file_path: str, *, strict: bool = False) -> ExtractedTags:
    """Open a file, apply the read-error policy, and construct extracted metadata.

    Non-strict parse failures degrade to an empty result, while direct or mutagen-wrapped I/O
    errors propagate. Strict verification wraps every open/parse failure in :class:`TagReadError`.
    A recognized file with no tags is valid and still returns duration/bitrate metadata.
    """
    try:
        audio = mutagen.File(file_path)
    except Exception as exc:
        return _read_failure_result(file_path, strict, exc)

    if audio is None:
        if strict:
            msg = f"{file_path} is not a recognized audio file on re-read"
            raise TagReadError(msg)
        return ExtractedTags()

    info = getattr(audio, "info", None)
    length = getattr(info, "length", None)
    duration = float(length) if length is not None and length > 0 else None
    bitrate_value = getattr(info, "bitrate", None)
    bitrate = int(bitrate_value) if bitrate_value is not None else None

    tags = audio.tags
    raw_tags = _serialize_tags(tags)
    parsed = parse_format_tags(audio, tags)
    return ExtractedTags(
        artist=parsed.artist,
        title=parsed.title,
        album=parsed.album,
        year=parsed.year,
        genre=parsed.genre,
        track_number=parsed.track_number,
        duration=duration,
        bitrate=bitrate,
        raw_tags=raw_tags,
        raw_year=parsed.raw_year,
        raw_track_number=parsed.raw_track_number,
        raw_genre=parsed.raw_genre,
    )
