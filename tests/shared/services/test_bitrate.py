"""phaze-3aia5: the one bps -> kbps conversion shared by the record card and both dedupe surfaces."""

from __future__ import annotations

import pytest

from phaze.services.bitrate import bps_to_kbps
from phaze.services.dedup import _canonical_rationale
from phaze.services.review_dedupe import _format_quality


@pytest.mark.parametrize(("bps", "kbps"), [(128_000, 128), (320_000, 320), (64_040, 64), (127_999, 127), (999, 0)])
def test_bps_to_kbps_rounds_down(bps: int, kbps: int) -> None:
    assert bps_to_kbps(bps) == kbps


def test_the_dedupe_surfaces_render_through_the_shared_conversion() -> None:
    """Unchanged by phaze-3aia5: the same stored bps renders the same kbps on both dedupe surfaces."""
    assert _format_quality({"file_size": None, "bitrate": 64_040}).startswith("64 kbps")
    rationale = _canonical_rationale({"bitrate": 64_040}, {"bitrate": 32_000})
    assert rationale == "highest bitrate (64kbps)"
