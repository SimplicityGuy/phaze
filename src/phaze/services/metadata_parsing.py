"""Pure format-specific normalization for opened mutagen audio/tag objects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from phaze.services.pg_text import sanitize_pg_text
from phaze.services.tag_formats import TagFormat, UnsupportedTagFormatError, resolve_tag_format
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

# phaze-wt9vw. ASF (.wma) attributes. Until this existed, ASF files fell through the dispatch below
# into ``_VORBIS_MAP``, which meant phaze read all six fields as ``None`` from any .wma tagged by a
# conformant tagger -- a silent all-empty INGEST, not merely a verify-side problem. The write-side
# half of the same defect wrote Vorbis keys into ASF and then confirmed them through this very
# fallback.
#
# Written independently of ``tag_write_disk._WRITE_ASF_MAP`` rather than shared with it, on purpose:
# two separately-authored tables make a typo in either one visible to a round-trip test, which a
# single shared table would hide from every test that does not consult an external reader. Their
# mutual consistency is asserted by
# ``tests/review/services/test_tag_write_real_containers.py::test_write_and_read_maps_are_mutual_inverses``.
_ASF_MAP: dict[str, str] = {
    "Author": "artist",
    "Title": "title",
    "WM/AlbumTitle": "album",
    "WM/Year": "year",
    "WM/Genre": "genre",
    "WM/TrackNumber": "track_number",
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
    """Extract the first string from a tag value.

    Handles lists, ID3 text frames, and plain strings. Runs the result through
    :func:`~phaze.services.text_repair.repair_mojibake` (phaze-x4ux) -- this is the ingest
    boundary for artist/title/album/genre, the ONE place a mis-decoded tag (e.g. a UTF-8
    filename or comment misread as cp1252/latin-1 upstream) gets persisted, so repairing here
    means every downstream reader (search, tracklist matching, rename proposals) sees clean text
    without needing its own repair call. Conservative and a no-op on already-clean text -- see
    the module's own safety-gate documentation.
    """
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
    """Clamp a parsed track number to the wire contract's domain (phaze-0do9).

    Mirrors ``_parse_year``'s sanity check above: ``MetadataWriteRequest.track_number``
    (schemas/agent_metadata.py) is ``Field(ge=0, le=9999)``, guarding an int4 column. Degrading
    an out-of-domain value to ``None`` here -- instead of letting it ride to the wire boundary --
    keeps one junk field from sinking the whole metadata row: previously a TRCK tag like
    "20211013" (a ripper stuffing a date into the track frame) or a bogus "-1"/"10000" raised a
    pydantic ``ValidationError`` *inside* the extraction task's ``try`` block, which the generic
    ``except`` treated as a terminal metadata failure -- permanently losing that file's
    artist/title/album/year too.
    """
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
    """Preserve every value of a multi-value genre tag, as a real ``list[str]``.

    phaze-2zl7: unlike :func:`_first_str` (which keeps only the FIRST value -- correct for the
    normalized ``genre`` field used in search/matching), this preserves the full genre list for
    an undo snapshot, so reverting a write does not silently collapse a multi-genre tag down to
    one value.

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
        parts = [sanitize_pg_text(str(item)) for item in val if not isinstance(item, bytes)]
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else parts
    return sanitize_pg_text(str(val))


def _raw_track_text(val: Any) -> str | None:
    """Raw ``"N"`` / ``"N/total"`` text for a track-number tag value (phaze-2zl7).

    Unlike :func:`_parse_track` (which keeps only the leading number, discarding a "/total"
    suffix), this preserves the total for an undo snapshot. Handles the same shapes
    ``_parse_track`` does: a plain string ("3", "3/12"), an MP4 ``trkn`` tuple/list-of-tuple
    (``(3, 12)`` / ``[(3, 12)]``), and ``None``.

    phaze-6p7fz: an in-domain track 0 (e.g. ``(0, 12)``) round-trips to raw text ("0/12") the
    same way :func:`_parse_track` parses it (0, not None) -- keeping the phaze-2zl7 "raw is None
    iff normalized is None" contract intact. See :func:`_raw_track_tuple` for the deliberate
    asymmetry between the two tuple components.

    Deliberate divergence from the pre-refactor implementation: a list wrapping ``None``
    (``[None]``, unreachable from a real mutagen tag) now returns ``None`` instead of the
    literal string ``"None"`` -- restoring the phaze-2zl7 "raw is None iff normalized is None"
    contract for this input rather than preserving the old bug. Pinned by
    ``test_raw_track_text_list_of_none_is_none_not_the_string_none``.
    """
    value = _first_track_value(val)
    if value is None:
        return None
    if isinstance(value, tuple):
        return _raw_track_tuple(value)
    text = sanitize_pg_text(str(value)).strip()
    return text or None


def _raw_track_tuple(value: tuple[Any, ...]) -> str | None:
    """Render an MP4 ``trkn`` tuple without losing a valid zero track.

    The two components are checked differently, on purpose (phaze-6p7fz). ``value[0]`` (the
    track number) is checked with ``is not None``, not truthiness, so a valid track 0 still
    renders. ``value[1]`` (the total) keeps its truthiness check: MP4 rippers use a total of 0
    to mean "no total" (e.g. ``(3, 0)``), so a 0 total is still omitted from the "/total" suffix
    -- do not "fix" this into a matching ``is not None`` check on ``value[1]``, it would render a
    bogus "/0" suffix.
    """
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
    """Purely normalize opened mutagen objects according to their tag family.

    phaze-wt9vw: the family is decided by the SHARED :func:`~phaze.services.tag_formats.resolve_tag_format`,
    the same call ``tag_write_disk.write_tags`` dispatches on. Previously this function carried its
    own copy of that decision, ending in an unguarded ``return _mapped_sources(tags, _VORBIS_MAP, ...)``
    -- so any container phaze did not recognise was read as though it were Vorbis. Paired with the
    identical fallback on the write side, that is what let a .wma be written with Vorbis keys and
    then VERIFIED against those same keys: two independent guesses that always agree.

    An unresolvable container yields an empty result rather than raising. That is deliberate and is
    NOT the old fallback in disguise: ingest opens whatever the archive contains, and the honest
    answer for a container phaze cannot read is "no tags", which is also what the old Vorbis
    fallback produced for one (barring an accidental key collision -- precisely the ASF case). The
    WRITE path, where a wrong guess corrupts a file instead of returning nothing, refuses instead.
    """
    if tags is None:
        return ParsedTagValues()
    try:
        tag_format = resolve_tag_format(audio, tags)
    except UnsupportedTagFormatError:
        return ParsedTagValues()

    if tag_format is TagFormat.ID3:
        return _mapped_sources(tags, _ID3_MAP, _id3_value, _text_value)
    if tag_format is TagFormat.MP4:
        return _mapped_sources(tags, _MP4_MAP, lambda _field, value: value, _mp4_value)
    if tag_format is TagFormat.ASF:
        return _mapped_sources(tags, _ASF_MAP, lambda _field, value: value, _text_value)
    return _mapped_sources(tags, _VORBIS_MAP, lambda _field, value: value, _text_value)
