"""Tag proposal service - computes merged tags from multiple sources.

Priority cascade (per field, independently):
  discogs_link > tracklist > FileMetadata > filename parsing
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from phaze.models.discogs_link import DiscogsLink
    from phaze.models.metadata import FileMetadata
    from phaze.models.tracklist import Tracklist

CORE_FIELDS = ("artist", "title", "album", "year", "genre", "track_number")

_YEAR_RE = re.compile(r"\((\d{4})\)")


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
    file_metadata: FileMetadata | None,
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
    # Layer 1: filename parsing (lowest priority)
    merged = parse_filename(filename)

    # Layer 2: FileMetadata (overwrites filename for non-None fields)
    if file_metadata is not None:
        for field in CORE_FIELDS:
            val = getattr(file_metadata, field, None)
            if val is not None:
                merged[field] = val

    # Layer 3: Tracklist
    if tracklist is not None:
        if tracklist.artist is not None:
            merged["artist"] = tracklist.artist
        if tracklist.event is not None:
            merged["album"] = tracklist.event
        # Tracklist date -> year is FALLBACK only (does not override existing year)
        if tracklist.date is not None and "year" not in merged:
            merged["year"] = tracklist.date.year

    # Layer 4: Accepted DiscogsLink (highest priority -- verified metadata)
    if discogs_link is not None:
        if discogs_link.discogs_artist is not None:
            merged["artist"] = discogs_link.discogs_artist
        if discogs_link.discogs_title is not None:
            merged["title"] = discogs_link.discogs_title
        if discogs_link.discogs_year is not None:
            merged["year"] = discogs_link.discogs_year

    # Filter to core fields only and remove None values
    return {k: v for k, v in merged.items() if k in CORE_FIELDS and v is not None}
