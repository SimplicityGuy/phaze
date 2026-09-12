"""Changes-review scenarios moved from ``tests/review/services/test_review_formatters.py``."""

import pytest

from phaze.services.review import _format_quality, _format_size


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
