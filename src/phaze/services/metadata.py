"""Tag extraction service using mutagen for audio metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
import structlog

from phaze.services.pg_text import sanitize_pg_text


logger = structlog.get_logger(__name__)


class TagReadError(Exception):
    """Raised by :func:`extract_tags` in ``strict`` mode when a file cannot be read/parsed.

    The default (non-strict) reader swallows PARSE failures into an all-``None``
    :class:`ExtractedTags` (I/O failures propagate as ``OSError`` -- phaze-todn), which is
    correct for best-effort ingestion but wrong for verify-after-write: there a swallowed
    re-read is indistinguishable from a file that genuinely has no tags (phaze-vq3g).
    ``strict=True`` raises this instead so the caller can tell "could not re-read" apart
    from "tags absent".
    """


def _io_cause(exc: BaseException) -> OSError | None:
    """Return the ``OSError`` in *exc*'s explicit cause chain, if any (phaze-todn).

    mutagen wraps open/read failures in ``MutagenError`` (raised ``from`` the original
    ``OSError``), so classifying an extraction failure as I/O-vs-parse requires walking
    ``__cause__``. Only the explicit chain is inspected -- ``__context__`` can carry an
    unrelated exception that happened to be in flight.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError):
            return current
        current = current.__cause__
    return None


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


# Characters PostgreSQL cannot accept in a UTF8 text/jsonb column (PostgreSQL §8.14):
#   - U+0000 (NUL): rejected by both text and jsonb columns.
#   - U+D800-U+DFFF (Unicode surrogates): rejected by jsonb, and unencodable to UTF-8
#     when asyncpg transmits a text value. In a Python ``str`` astral characters are
#     single code points (never stored as surrogate pairs), so any code point in this
#     range is necessarily a LONE surrogate -- stripping the whole range is safe.
#
# The implementation now lives in the stdlib-only ``services/pg_text`` module so the control-plane
# routers can reuse it without importing ``mutagen`` through this module. Aliased here to keep this
# module's existing call sites unchanged.
_sanitize_pg_text = sanitize_pg_text


def _first_str(val: Any) -> str | None:
    """Extract the first string from a tag value.

    Handles lists, ID3 text frames, and plain strings.
    """
    if val is None:
        return None
    if isinstance(val, list):
        return _sanitize_pg_text(str(val[0])) if val else None
    return _sanitize_pg_text(str(val))


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
        str_key = _sanitize_pg_text(str(key))
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
                    serialized.append(_sanitize_pg_text(str(item)))
                if serialized:
                    result[str_key] = serialized
            else:
                result[str_key] = _sanitize_pg_text(str(val))
        except Exception:
            logger.debug("Failed to serialize tag %s", str_key)
            continue

    return result


def extract_tags(file_path: str, *, strict: bool = False) -> ExtractedTags:
    """Extract audio tags from a file using mutagen.

    Returns an ExtractedTags dataclass with normalized fields and raw tag dump.
    A file that opens cleanly but is unrecognized or carries no/unparseable tags
    returns ExtractedTags with all None fields and an empty raw_tags dict.

    An I/O failure (``OSError``, incl. ``FileNotFoundError`` -- the file is missing, or
    the media mount hiccuped) PROPAGATES instead of degrading to an empty result
    (phaze-todn): swallowing it here made the metadata stage report a successful
    all-``None`` extraction for a file it never read, permanently masking the failure
    from the task's terminal-failure/retry machinery. Callers that want the degrade
    behavior must catch ``OSError`` explicitly.

    Args:
        file_path: Path to the audio file.
        strict: When ``True``, an open/parse failure (or an unrecognized-format file) raises
            :class:`TagReadError` instead of being swallowed into an all-``None`` result. Verify
            paths use this so a re-read failure is distinguishable from genuinely-absent tags
            (phaze-vq3g). A file that opens cleanly but carries no tags is NOT an error -- it
            returns an all-``None`` result in both modes.

    Raises:
        OSError: The file could not be READ (non-strict mode). Distinct from "the file has
            no tags", which stays a successful empty extraction.
        TagReadError: Any open/parse failure in ``strict`` mode (phaze-vq3g).
    """
    try:
        audio = mutagen.File(file_path)
    except Exception as exc:
        if strict:
            msg = f"failed to read tags from {file_path}: {exc}"
            raise TagReadError(msg) from exc
        if isinstance(exc, OSError):
            # phaze-todn: a read failure is NOT 'no tags' -- let the caller's failure/retry
            # machinery run instead of recording an empty successful extraction.
            raise
        io_error = _io_cause(exc)
        if io_error is not None:
            # mutagen wraps open/read OSErrors in MutagenError (which does NOT subclass
            # OSError), so unwrap and re-raise the underlying I/O failure -- same
            # phaze-todn rule as the direct-OSError branch above.
            raise io_error from exc
        # The file was readable but mutagen could not parse it (corrupt/exotic tag data):
        # a genuinely-unparseable-tags case, kept as an empty successful extraction.
        logger.debug("Failed to parse tags with mutagen: %s", file_path)
        return ExtractedTags()

    if audio is None:
        if strict:
            msg = f"{file_path} is not a recognized audio file on re-read"
            raise TagReadError(msg)
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
