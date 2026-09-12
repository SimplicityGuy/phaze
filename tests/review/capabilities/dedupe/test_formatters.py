"""Dedupe-review scenarios moved from ``tests/review/services/test_review_formatters.py``."""

from phaze.services.review import build_dupe_group_card, dedupe_subcount_text


def test_build_dupe_group_card_marks_exactly_the_canonical_file_as_keeper() -> None:
    """``keeper`` is ``id == canonical_id`` -- the flag the keeper radio in ``_dupe_group.html`` binds to."""
    card = build_dupe_group_card(
        {
            "sha256_hash": "abc123def456789",
            "canonical_id": 2,
            "truncated": False,
            "files": [
                {"id": 2, "original_path": "/archive/<set-01>/keeper.mp3", "file_size": 23488102, "bitrate": 320000},
                {"id": 7, "original_path": "/archive/<set-02>/dup.mp3", "file_size": 1024, "bitrate": None},
            ],
        }
    )

    assert card["sha256_hash"] == "abc123def456789"
    assert card["group_name"] == "keeper.mp3"  # the FIRST file's basename (score_group sorts keeper-first)
    assert card["count"] == 2
    assert card["truncated"] is False
    assert [f["keeper"] for f in card["files"]] == [True, False]
    assert [f["name"] for f in card["files"]] == ["keeper.mp3", "dup.mp3"]
    assert [f["quality"] for f in card["files"]] == ["320 kbps · 22.4 MB", "1.0 KB"]


def test_build_dupe_group_card_labels_an_empty_group_with_a_hash_prefix() -> None:
    """With no files there is no basename to label with, so the card falls back to a 12-char hash prefix."""
    card = build_dupe_group_card({"sha256_hash": "abc123def456789", "canonical_id": None, "files": []})

    assert card["group_name"] == "abc123def456"
    assert card["count"] == 0
    assert card["files"] == []


def test_build_dupe_group_card_degrades_a_missing_truncated_key_to_false() -> None:
    """phaze-z4p5q: a caller that built the group dict some other way must degrade, not raise."""
    card = build_dupe_group_card(
        {
            "sha256_hash": "abc123def456789",
            "canonical_id": 1,
            "files": [{"id": 1, "original_path": "/archive/<set-01>/only.mp3", "file_size": None, "bitrate": None}],
        }
    )

    assert card["truncated"] is False
    assert card["files"][0]["quality"] == "unknown size"


def test_dedupe_subcount_text_reports_a_plain_count_when_nothing_is_truncated() -> None:
    """rendered == total: no "Showing X of Y" hedge, just the honest count."""
    assert dedupe_subcount_text(3, 3) == "3 duplicate groups · pick the keeper, others archived"


def test_dedupe_subcount_text_singularizes_one_group() -> None:
    assert dedupe_subcount_text(1, 1) == "1 duplicate group · pick the keeper, others archived"


def test_dedupe_subcount_text_reports_zero_groups() -> None:
    assert dedupe_subcount_text(0, 0) == "0 duplicate groups · pick the keeper, others archived"


def test_dedupe_subcount_text_flags_a_bounded_render() -> None:
    """phaze-4iq5t AC1(b): rendered < total must say so explicitly -- the minimum bar an operator
    needs to know groups exist beyond what's on screen."""
    assert dedupe_subcount_text(100, 220) == "Showing 100 of 220 duplicate groups · pick the keeper, others archived"
