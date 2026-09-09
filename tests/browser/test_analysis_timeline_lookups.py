"""phaze-x1qr3.10: unit tests for the three pure lookups in ``static/js/analysis_timeline.js``.

Executed in a real browser against the SHIPPED file, because that is the artifact's real
consumer. This repo has no node and no jsdom, and ADR-0012 rule 3 rules out the alternative
that needs neither: a Python port of the same arithmetic would round, compare and short-circuit
in Python, and a floating-point boundary rule that held there would say nothing about the one
that runs in Chromium. The functions are exported on ``window.PhazeAnalysisTimeline`` for
exactly this; ``tests/shared/static/test_analysis_timeline_js.py`` pins that export so a rename
breaks the DEFAULT suite rather than quietly disarming this file.

Every case here is one of the three the bead names -- a boundary, inside a gap, and at the file
end -- because those are where a half-open range rule is decided and where an off-by-one is
invisible in the middle of a window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser


# One synthetic file, 120 s long, with a deliberate hole between 60 s and 90 s in every series.
_DURATION = 120.0
_WINDOWS = [
    {"start": 0.0, "end": 30.0, "index": 0},
    {"start": 30.0, "end": 60.0, "index": 1},
    {"start": 90.0, "end": 120.0, "index": 2},
]
# The first track starts at 10 s, so there is a real "before any track" region no segment covers
# -- a scraped tracklist whose first timestamp is not 0:00 is ordinary, not hypothetical. The
# last carries `end: null`, the open-ended final segment of a file whose duration is unknown.
_SEGMENTS = [
    {"position": 1, "title": "Opening Track", "start": 10.0, "end": 40.0},
    {"position": 2, "title": "Middle Track", "start": 40.0, "end": 100.0},
    {"position": 3, "title": None, "start": 100.0, "end": None},
]
_KEY_RUNS = [
    {"index": 0, "code": "8A", "number": 8, "start": 0.0, "end": 60.0},
    {"index": 1, "code": "5A", "number": 5, "start": 90.0, "end": 120.0},
]


async def _open_host(page: Any, seed: Seeder) -> None:
    """Load any page that ships ``analysis_timeline.js``, so its exports are reachable.

    The record page is the natural host. Which file it shows is irrelevant here: these tests
    call the lookups on their OWN synthetic spans, so the page supplies the runtime and nothing
    else -- which is the point of testing the functions rather than the rendering.
    """
    file = await seed.file(filename="<set-01>.mp3")
    await seed.metadata(file, duration=_DURATION)
    await seed.analysis_windows(file, fine_count=4, coarse_count=1)
    await page.goto(f"/files/{file.id}", wait_until="domcontentloaded")
    await page.wait_for_function("() => Boolean(window.PhazeAnalysisTimeline)")


async def _lookup(page: Any, name: str, spans: list[dict[str, Any]], time: float) -> Any:
    """Call one exported lookup in the page and return its result as plain data."""
    return await page.evaluate(
        "([name, spans, time, duration]) => window.PhazeAnalysisTimeline[name](spans, time, duration) ?? null",
        [name, spans, time, _DURATION],
    )


async def test_measured_window_resolves_boundaries_gaps_and_the_final_instant(page: Any, seed: Seeder) -> None:
    """A window's range is half-open, except at the file's own end.

    The boundary case is the one that decides which of two touching windows the reader is told
    about: at exactly 30 s the cursor is in the SECOND window, because the first one's 30 s edge
    is where it stopped measuring. The file-end case is the deliberate exception -- ``End`` and a
    pointer at the right edge both land exactly on the duration, and a strictly half-open rule
    would report "no measured window" at the last instant of a fully measured file.
    """
    await _open_host(page, seed)

    assert (await _lookup(page, "measuredWindow", _WINDOWS, 29.999))["index"] == 0
    assert (await _lookup(page, "measuredWindow", _WINDOWS, 30.0))["index"] == 1
    assert (await _lookup(page, "measuredWindow", _WINDOWS, 59.999))["index"] == 1

    # Inside the hole: the window before it has ended and the next has not begun. Nothing
    # measured here, and reporting the nearest window would draw coverage the archive lacks.
    assert await _lookup(page, "measuredWindow", _WINDOWS, 60.0) is None
    assert await _lookup(page, "measuredWindow", _WINDOWS, 75.0) is None
    assert await _lookup(page, "measuredWindow", _WINDOWS, 89.999) is None

    assert (await _lookup(page, "measuredWindow", _WINDOWS, 120.0))["index"] == 2
    # Before the first window there is no candidate at all -- a different code path from a gap.
    assert await _lookup(page, "measuredWindow", [{"start": 5.0, "end": 10.0, "index": 0}], 1.0) is None


async def test_segment_at_resolves_boundaries_the_pre_first_track_gap_and_an_open_ended_last(page: Any, seed: Seeder) -> None:
    """Track spans are half-open and consecutive, and the last one may have no end at all."""
    await _open_host(page, seed)

    assert (await _lookup(page, "segmentAt", _SEGMENTS, 39.999))["position"] == 1
    assert (await _lookup(page, "segmentAt", _SEGMENTS, 40.0))["position"] == 2

    # Before the first scraped timestamp: the set is playing, but no track claims this time.
    assert await _lookup(page, "segmentAt", _SEGMENTS, 0.0) is None
    assert await _lookup(page, "segmentAt", _SEGMENTS, 9.999) is None

    # `end: null` is bounded by the duration, so the final instant is inside the last track.
    assert (await _lookup(page, "segmentAt", _SEGMENTS, 119.0))["position"] == 3
    assert (await _lookup(page, "segmentAt", _SEGMENTS, 120.0))["position"] == 3

    # A zero-length segment -- what a non-monotonic pair of scraped timestamps collapses to --
    # contains no time at all, so the reader is told nothing rather than told a bad boundary.
    zero = [{"position": 1, "title": None, "start": 50.0, "end": 50.0}]
    assert await _lookup(page, "segmentAt", zero, 50.0) is None


async def test_key_run_at_resolves_boundaries_gaps_and_the_final_instant(page: Any, seed: Seeder) -> None:
    """The run's ``index`` is the wheel node the ring is placed on, so it must survive the lookup."""
    await _open_host(page, seed)

    assert (await _lookup(page, "keyRunAt", _KEY_RUNS, 0.0))["index"] == 0
    assert (await _lookup(page, "keyRunAt", _KEY_RUNS, 59.999))["code"] == "8A"

    # Key runs, unlike track segments, are NOT consecutive: a stretch with no measured key is a
    # real hole, and the ring must go out over it rather than hold the last key it saw.
    assert await _lookup(page, "keyRunAt", _KEY_RUNS, 60.0) is None
    assert await _lookup(page, "keyRunAt", _KEY_RUNS, 75.0) is None

    assert (await _lookup(page, "keyRunAt", _KEY_RUNS, 90.0))["index"] == 1
    assert (await _lookup(page, "keyRunAt", _KEY_RUNS, 120.0))["index"] == 1
    assert await _lookup(page, "keyRunAt", [], 30.0) is None
