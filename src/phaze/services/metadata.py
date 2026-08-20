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


def _read_failure_result(file_path: str, strict: bool, exc: Exception) -> ExtractedTags:
    """Apply strict-vs-ingest and I/O-vs-parse policy to one mutagen open failure."""
    if strict:
        msg = f"failed to read tags from {file_path}: {exc}"
        raise TagReadError(msg) from exc
    if isinstance(exc, OSError):
        # phaze-todn: a read failure is NOT 'no tags' -- let the caller's failure/retry
        # machinery run instead of recording an empty successful extraction. Bare `raise`
        # (not `raise exc from exc`): re-raising `exc` as its own cause makes
        # `exc.__cause__ is exc`, a self-referential chain that any `__cause__` walker
        # without a cycle guard loops on forever.
        raise
    io_error = _io_cause(exc)
    if io_error is not None:
        # mutagen wraps open/read OSErrors in MutagenError (which does NOT subclass
        # OSError), so unwrap and re-raise the underlying I/O failure -- same phaze-todn
        # rule as the direct-OSError branch above.
        raise io_error from exc
    # The file was readable but mutagen could not parse it (corrupt/exotic tag data):
    # a genuinely-unparseable-tags case, kept as an empty successful extraction.
    logger.debug("Failed to parse tags with mutagen: %s", file_path)
    return ExtractedTags()


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
    # ``year``/``track_number``/``genre`` above discard information ``_parse_year``/``_parse_track``/
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
    """Extract audio tags from a file using mutagen.

    Returns an ExtractedTags dataclass with normalized fields and raw tag dump. A file that opens
    cleanly but is unrecognized or carries no/unparseable tags returns ExtractedTags with all
    None fields and an empty raw_tags dict.

    An I/O failure (``OSError``, incl. ``FileNotFoundError`` -- the file is missing, or the
    media mount hiccuped) PROPAGATES instead of degrading to an empty result (phaze-todn):
    swallowing it here made the metadata stage report a successful all-``None`` extraction for a
    file it never read, permanently masking the failure from the task's terminal-failure/retry
    machinery. Callers that want the degrade behavior must catch ``OSError`` explicitly.

    Args:
        file_path: Path to the audio file.
        strict: When ``True``, an open/parse failure (or an unrecognized-format file) raises
            :class:`TagReadError` instead of being swallowed into an all-``None`` result. Verify
            paths use this so a re-read failure is distinguishable from genuinely-absent tags
            (phaze-vq3g). A file that opens cleanly but carries no tags is NOT an error -- it
            returns an all-``None`` result in both modes.

    Raises:
        OSError: The file could not be READ (non-strict mode). Distinct from "the file has no
            tags", which stays a successful empty extraction.
        TagReadError: Any open/parse failure in ``strict`` mode (phaze-vq3g).
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
