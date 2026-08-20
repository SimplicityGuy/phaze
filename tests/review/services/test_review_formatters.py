"""Direct unit tests for ``services/review.py``'s pure card/label formatters.

WHY THIS FILE EXISTS (phaze-k309t follow-up). ``services/review.py`` carries repowise's
``untested_hotspot`` biomarker and the worst defect score in the repo (1.06/10), which reads as
"this module has no tests". Measured, it does not hold: the module is at **99.35% line / 97.93%
branch** coverage, and all three uncovered branches are unreachable by construction --

* ``get_cue_review_cards``'s ``len(cards) >= _MAX_REVIEW_ROWS`` break, pre-empted by the SQL
  ``limit=_MAX_REVIEW_ROWS`` on ``_get_eligible_tracklist_query`` (one card is appended per row);
* ``get_tagwrite_review_page``'s ``while len(rows) < _MAX_REVIEW_ROWS`` false-exit, pre-empted by
  the in-loop ``break`` that fires the moment the cap is reached;
* ``get_cue_review_cards``'s ``if tracklist.latest_version_id:`` false arm -- the eligible query
  filters on ``latest_version_id.in_(has_timestamp_subq)``, and ``NULL IN (...)`` never matches,
  so an eligible row always carries a non-NULL ``latest_version_id``.

The biomarker is a MEASUREMENT artifact: repowise had no coverage report ingested for this repo
(``line_coverage_pct: null``) and found no paired test file, so it inferred "untested" from
absence of evidence. The systemic fix is ingesting coverage, not writing make-work tests.

What IS genuinely worth adding is this: the three pure formatters below were previously reached
only INDIRECTLY, through DB-seeded workspace reads. Pinning their contracts directly makes the
byte-level rendering rules (kbps conversion, the ``unknown size`` sentinel, the empty-group label
fallback) fail at their own call site instead of as a confusing diff in a rendered card.
"""

import pytest

from phaze.services.review import _format_quality, _format_size, build_dupe_group_card, dedupe_subcount_text


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (None, "unknown size"),
        (0, "unknown size"),  # falsy -> sentinel, NOT "0.0 B"
        (512, "512.0 B"),
        (1024, "1.0 KB"),
        (23488102, "22.4 MB"),
        (1024**3, "1.0 GB"),
        (1024**4, "1.0 TB"),
        (1024**5, "1.0 PB"),  # the post-loop fallthrough
    ],
)
def test_format_size_renders_each_unit_and_the_absent_sentinel(num_bytes: int | None, expected: str) -> None:
    """A byte count renders at the largest unit under 1024; absent/zero degrades to a sentinel, never "0.0 B"."""
    assert _format_size(num_bytes) == expected


def test_format_quality_divides_bitrate_by_1000_because_it_is_stored_in_bits() -> None:
    """phaze-iw2k: ``bitrate`` is stored in BITS per second (what mutagen reports), so kbps is //1000."""
    assert _format_quality({"file_size": 23488102, "bitrate": 320000}) == "320 kbps · 22.4 MB"


@pytest.mark.parametrize("bitrate", [None, 0])
def test_format_quality_omits_an_absent_bitrate_rather_than_rendering_zero_kbps(bitrate: int | None) -> None:
    """A missing or zero bitrate drops the kbps clause entirely -- "0 kbps" would be a lie about the file."""
    assert _format_quality({"file_size": 23488102, "bitrate": bitrate}) == "22.4 MB"


def test_format_quality_falls_back_to_the_size_sentinel_when_neither_field_is_present() -> None:
    """An empty file dict must still render a string -- ``get_dedupe_groups`` feeds this straight to a template."""
    assert _format_quality({}) == "unknown size"


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


# ---------------------------------------------------------------------------
# phaze-4iq5t: dedupe_subcount_text is the single source the Dedupe workspace's initial render
# (routers/shell.py) and its "Load more" fragment (routers/duplicates.py) both use, so the two
# can never independently drift on wording -- pinning the pure function here covers both call sites.
# ---------------------------------------------------------------------------


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
