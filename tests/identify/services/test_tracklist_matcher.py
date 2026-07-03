"""Tests for TracklistMatcher service -- pure logic, no mocking needed."""

from datetime import date

from phaze.services.tracklist_matcher import (
    AUTO_LINK_THRESHOLD,
    compute_match_confidence,
    parse_live_set_filename,
    should_auto_link,
)


class TestComputeMatchConfidence:
    """Tests for compute_match_confidence()."""

    def test_exact_match_returns_100(self):
        score = compute_match_confidence(
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
        )
        assert score == 100

    def test_exact_artist_event_date_off_5_days(self):
        score = compute_match_confidence(
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
            "Skrillex",
            "Coachella",
            date(2025, 4, 17),
        )
        # Date off by 5 days, but artist+event match > 80, so cap at 89
        assert 70 <= score <= 89

    def test_different_artist_returns_low(self):
        score = compute_match_confidence(
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
            "deadmau5",
            "Coachella",
            date(2025, 4, 12),
        )
        # Event and date match but artist differs -- score should be below auto-link
        assert score < AUTO_LINK_THRESHOLD

    def test_all_none_file_fields_returns_0(self):
        score = compute_match_confidence(
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
            None,
            None,
            None,
        )
        assert score == 0

    def test_date_cap_at_89_when_date_differs_more_than_3_days(self):
        """Artist+event similarity > 80 but date >3 days apart -> cap at 89."""
        score = compute_match_confidence(
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
            "Skrillex",
            "Coachella",
            date(2025, 5, 1),
        )
        assert score <= 89

    def test_partial_artist_match(self):
        score = compute_match_confidence(
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
            "Skrillex feat. Diplo",
            "Coachella",
            date(2025, 4, 12),
        )
        # Should still be high due to token_set_ratio
        assert score >= 80

    def test_none_tracklist_fields_returns_0(self):
        score = compute_match_confidence(
            None,
            None,
            None,
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
        )
        assert score == 0

    def test_date_within_3_days_no_cap(self):
        score = compute_match_confidence(
            "Skrillex",
            "Coachella",
            date(2025, 4, 12),
            "Skrillex",
            "Coachella",
            date(2025, 4, 14),
        )
        assert score >= 90


class TestParseLiveSetFilename:
    """Tests for parse_live_set_filename()."""

    def test_valid_format(self):
        result = parse_live_set_filename("Skrillex - Live @ Coachella 2025.04.12.mp3")
        assert result is not None
        artist, event, d = result
        assert artist == "Skrillex"
        assert event == "Coachella"
        assert d == date(2025, 4, 12)

    def test_invalid_format_returns_none(self):
        result = parse_live_set_filename("random_file.mp3")
        assert result is None

    def test_multi_word_artist(self):
        result = parse_live_set_filename("Above & Beyond - Live @ EDC Las Vegas 2025.06.20.mp3")
        assert result is not None
        artist, event, d = result
        assert artist == "Above & Beyond"
        assert event == "EDC Las Vegas"
        assert d == date(2025, 6, 20)

    def test_m4a_extension(self):
        result = parse_live_set_filename("Tiesto - Live @ Tomorrowland 2024.07.19.m4a")
        assert result is not None
        assert result[0] == "Tiesto"

    def test_no_extension_returns_none(self):
        result = parse_live_set_filename("Skrillex - Live @ Coachella 2025.04.12")
        assert result is None


class TestAutoLink:
    """Tests for auto-link threshold and should_auto_link()."""

    def test_threshold_is_90(self):
        assert AUTO_LINK_THRESHOLD == 90

    def test_should_auto_link_at_90(self):
        assert should_auto_link(90) is True

    def test_should_not_auto_link_at_89(self):
        assert should_auto_link(89) is False

    def test_should_auto_link_at_100(self):
        assert should_auto_link(100) is True

    def test_should_not_auto_link_at_0(self):
        assert should_auto_link(0) is False
