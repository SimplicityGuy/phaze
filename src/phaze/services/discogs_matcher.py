"""Discogsography API adapter and fuzzy matching for Discogs release linking."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from rapidfuzz import fuzz


if TYPE_CHECKING:
    from phaze.models.tracklist import TracklistTrack


logger = logging.getLogger(__name__)


class DiscogsographyClient:
    """HTTP client adapter for the discogsography service.

    Follows the same pattern as AudfprintAdapter/PanakoAdapter:
    create with base_url, call async methods, close when done.
    """

    def __init__(self, base_url: str = "http://discogsography:8000", timeout: float = 30.0) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def search_releases(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search Discogs releases via discogsography /api/search endpoint.

        Returns list of result dicts from the 'results' key.
        Gracefully handles ConnectError and TimeoutException by returning empty list.
        """
        try:
            resp = await self._client.get("/api/search", params={"q": query, "types": "release", "limit": limit})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            results: list[dict[str, Any]] = data.get("results", [])
            return results
        except httpx.ConnectError:
            logger.warning("Discogsography connection failed for query: %s", query)
            return []
        except httpx.TimeoutException:
            logger.warning("Discogsography request timed out for query: %s", query)
            return []

    async def close(self) -> None:
        """Close the httpx client."""
        await self._client.aclose()


def compute_discogs_confidence(track_artist: str, track_title: str, discogs_name: str, discogs_relevance: float) -> float:
    """Compute confidence score (0-100) blending rapidfuzz token_set_ratio and relevance.

    Weight: 0.6 string similarity + 0.4 API relevance score.
    """
    track_query = f"{track_artist} {track_title}".lower().strip()
    discogs_lower = discogs_name.lower().strip()

    # token_set_ratio returns 0-100, normalize to 0-1
    string_sim = fuzz.token_set_ratio(track_query, discogs_lower) / 100.0

    # Blend: string_sim (0.6) + relevance (0.4), scale to 0-100
    confidence = (string_sim * 0.6 + discogs_relevance * 0.4) * 100.0

    # Clamp to 0-100 range
    return round(min(100.0, max(0.0, confidence)), 1)


def _parse_artist_from_name(name: str) -> tuple[str | None, str]:
    """Best-effort parse 'Artist - Title' from a Discogs release name.

    Returns (artist, title). If no separator found, returns (None, name).
    """
    separators = [" - ", " \u2013 ", " \u2014 "]
    for sep in separators:
        if sep in name:
            parts = name.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return None, name


async def match_track_to_discogs(client: DiscogsographyClient, track: TracklistTrack) -> list[dict[str, Any]]:
    """Search discogsography for a single track and return top 3 scored candidates.

    Skips tracks with None/empty artist or title (D-02).
    Each result contains: discogs_release_id, discogs_artist, discogs_title,
    discogs_label, discogs_year, confidence.
    """
    if not track.artist or not track.title:
        return []

    query = f"{track.artist} {track.title}"
    results = await client.search_releases(query, limit=10)

    scored: list[dict[str, Any]] = []
    for result in results:
        name = result.get("name", "")
        relevance = result.get("relevance", 0.0)
        metadata = result.get("metadata", {})

        confidence = compute_discogs_confidence(track.artist, track.title, name, relevance)

        parsed_artist, parsed_title = _parse_artist_from_name(name)

        scored.append(
            {
                "discogs_release_id": result.get("id", ""),
                "discogs_artist": parsed_artist,
                "discogs_title": parsed_title,
                "discogs_label": metadata.get("label") if isinstance(metadata, dict) else None,
                "discogs_year": metadata.get("year") if isinstance(metadata, dict) else None,
                "confidence": confidence,
            }
        )

    # Sort by confidence descending, return top 3
    scored.sort(key=lambda x: x["confidence"], reverse=True)
    return scored[:3]
