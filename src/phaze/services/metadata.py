"""Tag extraction service using mutagen for audio metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
import structlog

from phaze.services.pg_text import sanitize_pg_text
from phaze.services.text_repair import repair_mojibake


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
    # phaze-2zl7: RAW (un-normalized) text for the three fields whose normalization is lossy --
    # ``year``/``track_number``/``genre`` below discard information ``_parse_year``/``_parse_track``/
    # ``_first_str`` intentionally trim for search/matching (a full release date truncates to a
    # 4-digit year, "N/total" drops the total, a multi-value genre keeps only the first). A caller
    # that needs to faithfully RESTORE what was on disk (an undo snapshot) should prefer these over
    # the normalized fields above; see ``tag_writer._extract_before_tags``. ``None`` iff the
    # corresponding normalized field is also ``None`` (the tag is genuinely absent).
    raw_year: str | None = None
    raw_track_number: str | None = None
    # phaze-z2u08: a MULTI-value genre tag (e.g. two Vorbis ``genre`` comments) is a ``list[str]``
    # here -- every value kept separate, never joined into one string -- so a write path can put
    # them back on disk as distinct frames/comments/atoms. A single-value genre stays a plain
    # ``str``, unchanged from before. See ``_raw_genre_value``.
    raw_genre: str | list[str] | None = None


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

    Handles lists, ID3 text frames, and plain strings. Runs the result through
    :func:`~phaze.services.text_repair.repair_mojibake` (phaze-x4ux) -- this is the ingest
    boundary for artist/title/album/genre, the ONE place a mis-decoded tag (e.g. a UTF-8 filename
    or comment misread as cp1252/latin-1 upstream) gets persisted, so repairing here means every
    downstream reader (search, tracklist matching, rename proposals) sees clean text without
    needing its own repair call. Conservative and a no-op on already-clean text -- see the
    module's own safety-gate documentation.
    """
    if val is None:
        return None
    if isinstance(val, list):
        return repair_mojibake(_sanitize_pg_text(str(val[0]))) if val else None
    return repair_mojibake(_sanitize_pg_text(str(val)))


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


def _bounded_track(n: int) -> int | None:
    """Clamp a parsed track number to the wire contract's domain (phaze-0do9).

    Mirrors ``_parse_year``'s sanity check immediately above: ``MetadataWriteRequest.track_number``
    (schemas/agent_metadata.py) is ``Field(ge=0, le=9999)``, guarding an int4 column. Degrading an
    out-of-domain value to ``None`` here -- instead of letting it ride to the wire boundary -- keeps
    one junk field from sinking the whole metadata row: previously a TRCK tag like "20211013" (a
    ripper stuffing a date into the track frame) or a bogus "-1"/"10000" raised a pydantic
    ValidationError *inside* the extraction task's try block, which the generic except treated as
    a terminal metadata failure -- permanently losing that file's artist/title/album/year too.
    """
    return n if 0 <= n <= 9999 else None


def _parse_track(val: Any) -> int | None:
    """Parse a track number from various formats.

    Handles: "3", "3/12", (3, 12), [(3, 12)], and invalid values. Every parsed value is
    clamped to the 0-9999 wire domain via :func:`_bounded_track` (phaze-0do9) -- an
    out-of-range or negative track number degrades to ``None`` instead of reaching the wire
    boundary, the same defensive-parse convention :func:`_parse_year` already follows.
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
                return _bounded_track(int(first[0])) if first[0] is not None else None
            except (ValueError, TypeError):
                return None
        val = first

    # Handle tuple directly
    if isinstance(val, tuple) and len(val) >= 1:
        try:
            return _bounded_track(int(val[0])) if val[0] is not None else None
        except (ValueError, TypeError):
            return None

    # Handle string: "3" or "3/12"
    text = str(val).strip()
    if "/" in text:
        text = text.split("/")[0].strip()
    try:
        return _bounded_track(int(text)) if text else None
    except ValueError:
        return None


def normalize_year_text(value: Any) -> int | None:
    """Public wrapper for :func:`_parse_year` (phaze-2zl7).

    Lets a caller outside this module (``tag_writer.verify_write``) compare a RAW year/date value
    against a re-read tag using the EXACT same normalization rule :func:`extract_tags` applies, so
    a faithful raw-text undo write (e.g. a full "2024-03-15" restored, re-read back as the int
    ``2024``) is not misreported as a discrepancy.
    """
    return _parse_year(str(value)) if value is not None else None


def normalize_track_number_text(value: Any) -> int | None:
    """Public wrapper for :func:`_parse_track`, for the same reason as :func:`normalize_year_text`."""
    return _parse_track(value)


def _raw_genre_value(val: Any) -> str | list[str] | None:
    """Preserve every value of a multi-value genre tag, as a real ``list[str]``.

    phaze-2zl7: unlike :func:`_first_str` (which keeps only the FIRST value -- correct for the
    normalized ``genre`` field used in search/matching), this preserves the full genre list for an
    undo snapshot, so reverting a write does not silently collapse a multi-genre tag down to one
    value.

    phaze-z2u08: this used to ``"; ".join`` the values into one string, which fixed the CAPTURE
    half of the undo snapshot but not the RESTORE half -- nothing on the write side ever split
    that joined string back apart, so undo wrote it as a single tag value anyway (and a naive
    ``split("; ")`` fix would be wrong too: a single genre value can legitimately contain "; ").
    Returning the values themselves, never re-joined into text, lets the write path put each one
    back on disk verbatim. A single-value genre still returns a plain ``str`` (unchanged
    behavior); ``None`` iff the tag is genuinely absent -- same contract as the sibling
    raw_year/raw_track_number helpers.
    """
    if val is None:
        return None
    if isinstance(val, list):
        parts = [_sanitize_pg_text(str(item)) for item in val if not isinstance(item, bytes)]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else parts
    return _sanitize_pg_text(str(val))


def _raw_track_text(val: Any) -> str | None:
    """Raw ``"N"`` / ``"N/total"`` text for a track-number tag value (phaze-2zl7).

    Unlike :func:`_parse_track` (which keeps only the leading number, discarding a "/total"
    suffix), this preserves the total for an undo snapshot. Handles the same shapes
    ``_parse_track`` does: a plain string ("3", "3/12"), an MP4 ``trkn`` tuple/list-of-tuple
    (``(3, 12)`` / ``[(3, 12)]``), and ``None``.

    phaze-6p7fz: the track-number component (``val[0]``) is checked with ``is not None``, not
    truthiness, so an in-domain track 0 (e.g. ``(0, 12)``) round-trips to raw text ("0/12") the
    same way :func:`_parse_track` now parses it (0, not None) -- keeping the phaze-2zl7 "raw is
    None iff normalized is None" contract intact. The total component (``val[1]``) keeps its
    truthiness check deliberately: MP4 rippers use a total of 0 to mean "no total" (e.g.
    ``(3, 0)``), so a 0 total is still omitted from the "/total" suffix.
    """
    if val is None:
        return None
    if isinstance(val, list):
        if not val:
            return None
        val = val[0]
    if isinstance(val, tuple):
        if len(val) >= 2 and val[1]:
            return f"{val[0]}/{val[1]}"
        if len(val) >= 1 and val[0] is not None:
            return str(val[0])
        return None
    text = _sanitize_pg_text(str(val)).strip()
    return text or None


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

    # phaze-2zl7: capture the RAW pre-normalization source value for genre/track_number alongside
    # ``fields`` (which already carries the raw, un-truncated string for "year" -- see below). Only
    # these two need a separate capture: _first_str already collapses a multi-value genre to its
    # first entry, and the MP4 branch keeps track_number as the raw tuple/list "as-is for
    # _parse_track", neither of which is directly usable as raw TEXT for an undo snapshot.
    raw_sources: dict[str, Any] = {}

    if isinstance(audio.tags, ID3):
        # ID3-tagged files (MP3, AIFF, etc.)
        for id3_key, field_name in _ID3_MAP.items():
            frame = audio.tags.get(id3_key)
            if frame is not None:
                raw_val = getattr(frame, "text", [frame])
                fields[field_name] = _first_str(raw_val)
                if field_name in ("genre", "track_number"):
                    raw_sources[field_name] = raw_val
    elif isinstance(audio, MP4):
        # MP4/M4A atoms
        for mp4_key, field_name in _MP4_MAP.items():
            val = audio.tags.get(mp4_key)
            if val is not None:
                if field_name == "track_number":
                    fields[field_name] = val  # Keep as-is for _parse_track
                else:
                    fields[field_name] = _first_str(val)
                if field_name in ("genre", "track_number"):
                    raw_sources[field_name] = val
    else:
        # Vorbis-style comments (OGG, FLAC, OPUS)
        for vorbis_key, field_name in _VORBIS_MAP.items():
            val = audio.tags.get(vorbis_key)
            if val is not None:
                fields[field_name] = _first_str(val)
                if field_name in ("genre", "track_number"):
                    raw_sources[field_name] = val

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
        # phaze-2zl7: "year" is already the raw, un-truncated date string here -- _parse_year (used
        # for the normalized field above) is the only thing that ever shortens it, and it is called
        # on a COPY (`fields.get("year")`), never mutating `fields` itself.
        raw_year=fields.get("year"),
        raw_track_number=_raw_track_text(raw_sources.get("track_number")),
        raw_genre=_raw_genre_value(raw_sources.get("genre")),
    )
