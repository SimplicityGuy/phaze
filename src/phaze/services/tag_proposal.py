"""Tag proposal service - computes merged tags from multiple sources.

Priority cascade (per field, independently):
  discogs_link > tracklist > FileMetadata > filename parsing
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import TYPE_CHECKING, Protocol, cast


if TYPE_CHECKING:
    from phaze.models.discogs_link import DiscogsLink
    from phaze.models.tracklist import Tracklist

CORE_FIELDS = ("artist", "title", "album", "year", "genre", "track_number")
_METADATA_FIELDS = tuple((field, field) for field in CORE_FIELDS)
_TRACKLIST_FIELDS = (("artist", "artist"), ("album", "event"))
_DISCOGS_FIELDS = (("artist", "discogs_artist"), ("title", "discogs_title"), ("year", "discogs_year"))


class TagFieldSource(Protocol):
    """Structural subset of ``FileMetadata`` -- exactly the :data:`CORE_FIELDS` attributes this
    module and ``routers.tags._build_comparison`` read via ``getattr(..., field, None)``.

    phaze-o2ln: lets a caller pass a plain, non-ORM snapshot (e.g. the bulk tag-write loop's
    ``_BulkCandidate.metadata``, a ``SimpleNamespace``) wherever a live ``FileMetadata`` relationship
    used to be required, so per-file DB session churn (a rollback expiring the ORM identity map)
    cannot invalidate data this function needs.
    """

    artist: str | None
    title: str | None
    album: str | None
    year: int | None
    genre: str | None
    track_number: int | None


_YEAR_RE = re.compile(r"\((\d{4})\)")


def _field_overlay(source: object | None, field_map: tuple[tuple[str, str], ...]) -> dict[str, str | int]:
    """Return the mapped, non-missing tag fields supplied by one source."""
    if source is None:
        return {}
    return {
        target_field: value
        for target_field, source_field in field_map
        if (value := cast("str | int | None", getattr(source, source_field, None))) is not None
    }


def parse_filename(filename: str) -> dict[str, str | int | None]:
    """Extract artist, title, and year from a filename.

    Supports patterns:
      - "Artist - Title.ext" -> {artist, title}
      - "Artist - Title (2024).ext" -> {artist, title, year}
      - "plain.ext" -> {} (no structured data extractable)
    """
    stem = PurePosixPath(filename).stem
    result: dict[str, str | int | None] = {}

    # Extract year from (YYYY) pattern
    year_match = _YEAR_RE.search(stem)
    if year_match:
        year_val = int(year_match.group(1))
        if 1000 <= year_val <= 9999:
            result["year"] = year_val
        # Remove year from stem for cleaner artist/title parsing
        stem = stem[: year_match.start()].strip()

    # Split on " - " for artist/title
    if " - " in stem:
        parts = stem.split(" - ", maxsplit=1)
        artist = parts[0].strip()
        title = parts[1].strip()
        if artist:
            result["artist"] = artist
        if title:
            result["title"] = title

    return result


def compute_proposed_tags(
    file_metadata: TagFieldSource | None,
    tracklist: Tracklist | None,
    filename: str,
    discogs_link: DiscogsLink | None = None,
) -> dict[str, str | int | None]:
    """Compute proposed tags by merging sources with priority cascade.

    Priority (per field, independently): DiscogsLink (accepted) > tracklist > FileMetadata > filename.

    Tracklist mapping:
      - tracklist.artist -> artist
      - tracklist.event -> album
      - tracklist.date.year -> year (fallback only)

    DiscogsLink mapping:
      - discogs_link.discogs_artist -> artist
      - discogs_link.discogs_title -> title
      - discogs_link.discogs_year -> year

    Returns dict with only non-None values, keys from CORE_FIELDS only.
    """
    # Apply sparse overlays from lowest to highest priority. Missing values are absent from an
    # overlay, so a source cannot erase a value supplied by an earlier source.
    merged = parse_filename(filename)
    merged.update(_field_overlay(file_metadata, _METADATA_FIELDS))

    # Tracklist dates are fallback-only, unlike the tracklist artist and event overlay.
    if tracklist is not None and tracklist.date is not None:
        merged.setdefault("year", tracklist.date.year)
    merged.update(_field_overlay(tracklist, _TRACKLIST_FIELDS))

    merged.update(_field_overlay(discogs_link, _DISCOGS_FIELDS))

    return {field: value for field, value in merged.items() if field in CORE_FIELDS and value is not None}
