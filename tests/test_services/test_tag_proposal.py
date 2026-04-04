"""Tests for the tag proposal service."""

from datetime import date
from unittest.mock import MagicMock

from phaze.services.tag_proposal import compute_proposed_tags, parse_filename


class TestParseFilename:
    """Tests for parse_filename function."""

    def test_artist_dash_title(self) -> None:
        result = parse_filename("Artist - Title.mp3")
        assert result["artist"] == "Artist"
        assert result["title"] == "Title"

    def test_artist_event_title_with_year(self) -> None:
        result = parse_filename("Artist - Event - Title (2024).mp3")
        assert result["year"] == 2024

    def test_plain_filename(self) -> None:
        result = parse_filename("untitled.mp3")
        # Should return minimal dict (possibly just title from stem)
        assert isinstance(result, dict)
        # No artist if no separator
        assert "artist" not in result or result.get("artist") is None

    def test_year_in_parentheses(self) -> None:
        result = parse_filename("Some Track (2019).ogg")
        assert result["year"] == 2019

    def test_no_year_returns_no_year_key(self) -> None:
        result = parse_filename("Artist - Title.mp3")
        assert "year" not in result

    def test_strips_extension(self) -> None:
        result = parse_filename("Artist - Title.flac")
        assert result["title"] == "Title"

    def test_empty_parts_ignored(self) -> None:
        result = parse_filename(" - .mp3")
        # Should not crash, empty parts filtered
        assert isinstance(result, dict)


class TestComputeProposedTags:
    """Tests for compute_proposed_tags function."""

    def _make_metadata(self, **kwargs: object) -> MagicMock:
        """Create a mock FileMetadata with given attributes."""
        meta = MagicMock()
        for field in ("artist", "title", "album", "year", "genre", "track_number"):
            setattr(meta, field, kwargs.get(field))
        return meta

    def _make_tracklist(self, **kwargs: object) -> MagicMock:
        """Create a mock Tracklist with given attributes."""
        tl = MagicMock()
        tl.artist = kwargs.get("artist")
        tl.event = kwargs.get("event")
        tl.date = kwargs.get("date")
        return tl

    def test_all_sources_tracklist_wins(self) -> None:
        meta = self._make_metadata(artist="Meta Artist", title="Meta Title", genre="Electronic")
        tl = self._make_tracklist(artist="TL Artist", event="Coachella 2024")
        result = compute_proposed_tags(meta, tl, "File Artist - File Title.mp3")
        assert result["artist"] == "TL Artist"
        assert result["album"] == "Coachella 2024"

    def test_metadata_only(self) -> None:
        meta = self._make_metadata(artist="Meta Artist", title="Meta Title", year=2023, genre="House")
        result = compute_proposed_tags(meta, None, "unknown.mp3")
        assert result["artist"] == "Meta Artist"
        assert result["title"] == "Meta Title"
        assert result["year"] == 2023
        assert result["genre"] == "House"

    def test_filename_only(self) -> None:
        result = compute_proposed_tags(None, None, "DJ Snake - Turn Down (2020).mp3")
        assert result["artist"] == "DJ Snake"
        assert result["title"] == "Turn Down"
        assert result["year"] == 2020

    def test_per_field_independence(self) -> None:
        """Tracklist artist wins, but FileMetadata genre is preserved."""
        meta = self._make_metadata(artist="Meta Artist", genre="Techno", title="Meta Title")
        tl = self._make_tracklist(artist="TL Artist")
        result = compute_proposed_tags(meta, tl, "file.mp3")
        assert result["artist"] == "TL Artist"
        assert result["genre"] == "Techno"
        assert result["title"] == "Meta Title"

    def test_tracklist_date_as_year_fallback(self) -> None:
        """Tracklist date provides year only when no other source has year."""
        tl = self._make_tracklist(artist="TL Artist", date=date(2024, 6, 15))
        result = compute_proposed_tags(None, tl, "file.mp3")
        assert result["year"] == 2024

    def test_tracklist_date_does_not_override_metadata_year(self) -> None:
        """If metadata has year, tracklist date does NOT override it."""
        meta = self._make_metadata(year=2020)
        tl = self._make_tracklist(date=date(2024, 6, 15))
        result = compute_proposed_tags(meta, tl, "file.mp3")
        assert result["year"] == 2020

    def test_none_values_omitted(self) -> None:
        result = compute_proposed_tags(None, None, "untitled.mp3")
        for val in result.values():
            assert val is not None

    def test_only_core_fields(self) -> None:
        meta = self._make_metadata(artist="A", title="T", album="Al", year=2020, genre="G", track_number=1)
        result = compute_proposed_tags(meta, None, "file.mp3")
        allowed = {"artist", "title", "album", "year", "genre", "track_number"}
        assert set(result.keys()).issubset(allowed)

    def _make_discogs_link(self, **kwargs: object) -> MagicMock:
        """Create a mock DiscogsLink with given attributes."""
        dl = MagicMock()
        dl.discogs_artist = kwargs.get("discogs_artist")
        dl.discogs_title = kwargs.get("discogs_title")
        dl.discogs_year = kwargs.get("discogs_year")
        return dl

    def test_discogs_link_overrides_tracklist(self) -> None:
        """Accepted DiscogsLink artist/title override tracklist values."""
        meta = self._make_metadata(artist="Meta Artist", genre="Electronic")
        tl = self._make_tracklist(artist="TL Artist", event="Coachella 2024")
        dl = self._make_discogs_link(discogs_artist="Discogs Artist", discogs_title="Discogs Title")
        result = compute_proposed_tags(meta, tl, "file.mp3", discogs_link=dl)
        assert result["artist"] == "Discogs Artist"
        assert result["title"] == "Discogs Title"
        assert result["album"] == "Coachella 2024"  # not overridden by discogs
        assert result["genre"] == "Electronic"  # from metadata

    def test_discogs_link_year_overrides(self) -> None:
        """DiscogsLink year overrides metadata year."""
        meta = self._make_metadata(year=2020)
        dl = self._make_discogs_link(discogs_year=2023)
        result = compute_proposed_tags(meta, None, "file.mp3", discogs_link=dl)
        assert result["year"] == 2023

    def test_discogs_link_none_fields_no_override(self) -> None:
        """DiscogsLink with None fields does not override lower-priority sources."""
        meta = self._make_metadata(artist="Meta Artist", title="Meta Title")
        dl = self._make_discogs_link(discogs_artist=None, discogs_title=None, discogs_year=None)
        result = compute_proposed_tags(meta, None, "file.mp3", discogs_link=dl)
        assert result["artist"] == "Meta Artist"
        assert result["title"] == "Meta Title"

    def test_discogs_link_without_other_sources(self) -> None:
        """DiscogsLink works as sole source of data."""
        dl = self._make_discogs_link(discogs_artist="Solo Artist", discogs_title="Solo Track", discogs_year=2025)
        result = compute_proposed_tags(None, None, "unknown.mp3", discogs_link=dl)
        assert result["artist"] == "Solo Artist"
        assert result["title"] == "Solo Track"
        assert result["year"] == 2025

    def test_no_discogs_link_unchanged_behavior(self) -> None:
        """Passing discogs_link=None produces same result as before."""
        meta = self._make_metadata(artist="Meta Artist", title="Meta Title")
        result_without = compute_proposed_tags(meta, None, "file.mp3")
        result_with_none = compute_proposed_tags(meta, None, "file.mp3", discogs_link=None)
        assert result_without == result_with_none
