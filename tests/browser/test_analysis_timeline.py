"""Real-browser contract for timeline inspection, overflow, and responsive presentation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.browser.helpers import open_shell, settled, settled_focus


if TYPE_CHECKING:
    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser


async def _open_record_page(page: Any, seed: Seeder, *, fine_count: int) -> tuple[Any, Any]:
    file = await seed.file(filename="<set-01>.mp3")
    await seed.metadata(file, duration=fine_count * 30.0)
    await seed.analysis_windows(file, fine_count=fine_count, coarse_count=max(1, fine_count // 4))
    await page.goto(f"/files/{file.id}", wait_until="domcontentloaded")
    timeline = page.locator("[data-analysis-timeline]")
    await timeline.wait_for(state="visible")
    await page.wait_for_function("() => document.querySelector('[data-analysis-timeline]').dataset.timelineReady === 'true'")
    return file, timeline


async def test_timeline_inspects_with_pointer_touch_and_keyboard_and_cues_overflow(page: Any, seed: Seeder) -> None:
    """One control exposes every input mode while the native scrollport owns overflow."""
    _file, timeline = await _open_record_page(page, seed, fine_count=24)
    inspector = timeline.locator("[data-timeline-inspector]")
    viewport = timeline.locator("[data-timeline-viewport]")
    frame = timeline.locator("[data-timeline-frame]")
    hint = timeline.locator("[data-timeline-scroll-hint]")
    readout = timeline.locator("[data-timeline-readout]")

    assert await timeline.locator('[tabindex="0"]').count() == 1
    assert await inspector.get_attribute("role") == "slider"
    assert await hint.is_visible()
    assert "scroll timeline" in (await hint.inner_text()).lower()
    assert await viewport.evaluate("el => el.scrollWidth > el.clientWidth")
    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

    polyline = timeline.locator("polyline").first
    assert await polyline.get_attribute("vector-effect") == "non-scaling-stroke"
    assert await polyline.get_attribute("stroke-linecap") == "round"
    assert await polyline.get_attribute("stroke-linejoin") == "round"
    assert await polyline.get_attribute("stroke-width") == "2"

    bounds = await inspector.bounding_box()
    assert bounds is not None
    await page.mouse.move(bounds["x"] + bounds["width"] * 0.3, bounds["y"] + 20)
    pointer_text = await readout.inner_text()
    assert "At " in pointer_text and "BPM" in pointer_text and "key" in pointer_text
    assert "mood" in pointer_text and "style" in pointer_text

    await inspector.dispatch_event(
        "pointerdown",
        {
            "clientX": bounds["x"] + bounds["width"] * 0.7,
            "clientY": bounds["y"] + 20,
            "pointerType": "touch",
            "pointerId": 7,
        },
    )
    assert float(await inspector.get_attribute("aria-valuenow") or "0") > 0
    assert "Coarse" in (await inspector.get_attribute("aria-valuetext") or "")

    # Re-running the public initializer must not add duplicate key handlers. One Right press moves
    # from zero to the first natural midpoint (15s), not two or three stops.
    await page.evaluate(
        """() => {
            const root = document.querySelector('[data-analysis-timeline]');
            window.PhazeAnalysisTimeline.initialize(root);
            window.PhazeAnalysisTimeline.initialize(root);
        }"""
    )
    await inspector.focus()
    await page.keyboard.press("Home")
    await page.keyboard.press("ArrowRight")
    assert await inspector.get_attribute("aria-valuenow") == "15"

    # A keyboard step schedules a SMOOTH scroll, which is still animating when the next statement
    # runs -- so a one-shot `scrollLeft = scrollWidth` can be overridden mid-flight, parking the
    # scroller in the middle with both affordances correctly lit. That reads as a bug in the
    # affordance and is a race in the driving, so drive it until it STAYS at the maximum. Measured
    # once as a real intermittent failure asserting "←" against "Scroll timeline ↔".
    await page.wait_for_function(
        """() => {
            const el = document.querySelector('[data-timeline-viewport]');
            const gutter = Math.max(0, el.offsetWidth - el.clientWidth);
            const max = el.scrollWidth - el.clientWidth - gutter;
            if (el.scrollLeft < max - 2) {
                el.scrollLeft = el.scrollWidth;
                el.dispatchEvent(new Event('scroll'));
                return false;
            }
            return true;
        }"""
    )
    await page.wait_for_function("() => document.querySelector('[data-timeline-frame]').classList.contains('timeline-can-scroll-left')")
    assert "←" in await hint.inner_text()
    assert not await frame.evaluate("el => el.classList.contains('timeline-can-scroll-right')")

    light_stroke = await polyline.evaluate("el => getComputedStyle(el).stroke")
    await page.evaluate("document.documentElement.classList.add('dark')")
    dark_stroke = await polyline.evaluate("el => getComputedStyle(el).stroke")
    assert light_stroke != dark_stroke

    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert await hint.is_visible()


async def test_short_timeline_has_no_false_overflow_cue(page: Any, seed: Seeder) -> None:
    """A short desktop timeline fits naturally and does not advertise nonexistent overflow."""
    _file, timeline = await _open_record_page(page, seed, fine_count=3)

    viewport = timeline.locator("[data-timeline-viewport]")
    hint = timeline.locator("[data-timeline-scroll-hint]")
    assert not await viewport.evaluate("el => el.scrollWidth > el.clientWidth + 2")
    assert await hint.is_hidden()


async def test_reserved_scrollbar_gutter_does_not_fake_room_to_the_right(page: Any, seed: Seeder) -> None:
    """A reserved gutter is not scrollable, so it must not keep the right-hand affordance lit.

    ``scrollWidth - clientWidth`` overstates the maximum scroll offset by the width of any reserved
    scrollbar gutter: the gutter is excluded from ``clientWidth`` yet cannot be scrolled into. macOS
    reports 0 for it because its scrollbars are overlays, so the naive form reads correct locally and
    lies by ~15px on Linux and Windows -- which is how the affordance shipped stuck-on for every user
    on those platforms while the suite stayed green on a developer machine.

    Two assertions, because the fix has two halves. The first -- that the shipped scrollport reserves
    no gutter at all -- runs everywhere and guards the CSS. The second re-reserves one and checks the
    affordance still reads "end", which guards the arithmetic in ``analysis_timeline.js``; it can only
    run where scrollbars are classic, so it skips on macOS rather than passing vacuously.
    """
    _file, timeline = await _open_record_page(page, seed, fine_count=24)
    frame = timeline.locator("[data-timeline-frame]")
    viewport = timeline.locator("[data-timeline-viewport]")
    hint = timeline.locator("[data-timeline-scroll-hint]")

    # The shipped stylesheet reserves no gutter, which is what keeps the naive form honest. Assert
    # that first: it is the CSS half of the fix, and it fails loudly if the declaration comes back.
    assert await viewport.evaluate("el => el.offsetWidth - el.clientWidth === 0"), (
        "the timeline scrollport must not reserve a scrollbar gutter (see app.css)"
    )

    await page.add_style_tag(content="[data-timeline-viewport] { scrollbar-gutter: stable; }")
    if await viewport.evaluate("el => el.offsetWidth - el.clientWidth") == 0:
        pytest.skip("overlay scrollbars reserve no gutter here (macOS); the defect is only reachable where scrollbars are classic, i.e. CI Linux")

    await viewport.evaluate("el => { el.scrollLeft = el.scrollWidth; el.dispatchEvent(new Event('scroll')); }")
    assert await viewport.evaluate("el => el.scrollLeft < el.scrollWidth - el.clientWidth"), (
        "a reserved gutter must shorten the reachable maximum below scrollWidth - clientWidth"
    )

    assert "←" in await hint.inner_text()
    assert not await frame.evaluate("el => el.classList.contains('timeline-can-scroll-right')")


async def test_drawer_swap_initializes_once_and_escape_still_closes_from_timeline(page: Any, seed: Seeder) -> None:
    """HTMX drawer swaps initialize the same control without consuming the host's Escape key."""
    file = await seed.file(filename="<set-01>.mp3")
    await seed.metadata(file, duration=720.0)
    await seed.analysis_windows(file, fine_count=24, coarse_count=6)
    await open_shell(page, "/s/files")
    await settled(page)

    opener = page.locator(f'#files-table-view .md\\:block button[hx-get="/record/{file.id}"]')
    await opener.click()
    timeline = page.locator("#record-body [data-analysis-timeline]")
    await timeline.wait_for(state="visible")
    await page.wait_for_function("() => document.querySelector('#record-body [data-analysis-timeline]').dataset.timelineReady === 'true'")
    inspector = timeline.locator("[data-timeline-inspector]")
    await page.evaluate(
        """() => {
            const root = document.querySelector('#record-body [data-analysis-timeline]');
            window.PhazeAnalysisTimeline.initialize(root);
            window.PhazeAnalysisTimeline.initialize(root);
        }"""
    )
    await inspector.focus()
    await page.keyboard.press("ArrowRight")
    assert await inspector.get_attribute("aria-valuenow") == "15"

    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.getElementById('record-body').checkVisibility()")
    assert await settled_focus(page, "aria-label") == f"Open details for {file.original_filename}"


_INSPECTION_DURATION_SEC = 720.0
# `analysis_windows(projected=True)` puts the unique energy maximum on coarse window 4 of 6,
# i.e. [480, 600), whose midpoint is 540 s. That is the RAW-window argmax -- what `energy_peak`
# returns, and what the resting cursor used to sit on. It is NOT what the page advertises since
# phaze-0zx26: the rest position now reads the STORED `set_profile.peak_sec`, the argmax over
# the 64-point resampled arc, which averages neighbours and lands elsewhere. So this constant is
# kept for exactly one job -- the value the rest position must NOT be. `_open_inspectable_record`
# asserts the seeded profile differs from it, which is what keeps this test able to tell the two
# definitions apart; the resting expectation itself is read from the profile, never written here.
_RAW_COARSE_PEAK_MIDPOINT_SEC = 540.0
# Two scraped tracks. The second deliberately does NOT start at the peak's own coarse boundary,
# so "focused the row" and "resting on the peak" are distinguishable states rather than the
# same number reached two ways.
_TRACK_TWO_START_SEC = 300.0
_TRACK_TWO_MIDPOINT_SEC = (_TRACK_TWO_START_SEC + _INSPECTION_DURATION_SEC) / 2
_TRACK_ONE_MIDPOINT_SEC = _TRACK_TWO_START_SEC / 2


async def _open_inspectable_record(page: Any, seed: Seeder) -> tuple[Any, Any, float]:
    """A record page carrying every inspection target: lanes, wheel, glyph and a tracklist.

    Every target is fed from the file's OWN stored rows -- the glyph through
    ``set_projection.build_profile``, the wheel through the same flicker filter the payload's
    key runs come from -- so a mark that lands on the wrong cell or the wrong node fails here
    rather than agreeing with a fixture that was built to match it.

    Returns the seeded profile's ``peak_sec`` alongside the page, because that stored value is
    the ONE quantity the rest position advertises (phaze-0zx26).
    """
    from datetime import date

    from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion

    file = await seed.file(filename="<set-01>.mp3")
    await seed.metadata(file, duration=_INSPECTION_DURATION_SEC)
    windows = await seed.analysis_windows(file, fine_count=24, coarse_count=6, projected=True)
    profile = await seed.set_profile(file, windows)
    # Read the rest position from the row the page reads, rather than restating it as a literal:
    # the assertion below is then about the page's rest position agreeing with the stored peak,
    # which is the claim, instead of about a number that happens to match today. The guard keeps
    # the fixture honest -- if the stored peak ever coincided with the raw-window argmax, every
    # resting assertion in this test would pass for a page that had gone back to the raw one.
    rest_sec = profile.peak_sec
    assert rest_sec is not None, "the seeded profile must carry a peak for the page to rest on"
    assert rest_sec != _RAW_COARSE_PEAK_MIDPOINT_SEC, "stored peak equals the raw-window argmax: this fixture no longer tells them apart"

    tracklist = Tracklist(
        external_id="inspection-set",
        source_url="https://example.test/tracklist",
        file_id=file.id,
        match_confidence=94,
        artist="Example Artist",
        event="Norwood Festival",
        date=date(2026, 8, 1),
    )
    seed.session.add(tracklist)
    await seed.session.flush()
    version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
    seed.session.add(version)
    await seed.session.flush()
    seed.session.add_all(
        [
            TracklistTrack(version_id=version.id, position=1, timestamp="0:00", artist="First Artist", title="Opening Track"),
            TracklistTrack(version_id=version.id, position=2, timestamp="5:00", artist="Second Artist", title="Closing Track"),
        ]
    )
    tracklist.latest_version_id = version.id
    await seed.session.commit()

    await page.goto(f"/files/{file.id}", wait_until="domcontentloaded")
    timeline = page.locator("[data-analysis-timeline]")
    await timeline.wait_for(state="visible")
    await page.wait_for_function("() => document.querySelector('[data-analysis-timeline]').dataset.timelineReady === 'true'")
    return file, timeline, rest_sec


def _percent(value: str) -> float:
    """A CSS percentage back to a number.

    Compared as numbers, never as strings: the browser round-trips ``style.left`` through its own
    serializer, which prints 41.6667% for the 41.66666666666667% that was assigned. A string
    comparison there asserts the serializer's precision, not the position.
    """
    assert value.endswith("%"), value
    return float(value[:-1])


async def _marks(page: Any) -> dict[str, Any]:
    """Everything the six non-textual marks are currently saying, read from the live DOM.

    Read as one snapshot rather than as six locator queries so the assertions below compare a
    single consistent state -- a partially applied update is exactly the defect worth catching,
    and six separate reads would paper over it.
    """
    return await page.evaluate(
        """() => {
            const span = document.querySelector('[data-timeline-track-span]');
            const ring = document.querySelector('[data-journey-cursor-ring]');
            const glyphCursor = document.querySelector('[data-set-glyph-cursor]');
            const currentRibbons = [...document.querySelectorAll('[data-timeline-lane="key"] .analysis-timeline-ribbon.is-current')];
            const currentRows = [...document.querySelectorAll('[data-track-row].is-current')];
            // The ring is an SVG <circle>, which has no `hidden` IDL property: `ring.hidden` reads
            // back whatever expando the script last assigned and says nothing about what is drawn.
            // Read the ATTRIBUTE and the computed display instead -- the pair is what the reader
            // sees (phaze-n0h86: the ring shipped invisible while `ring.hidden` read false).
            const ringHidden = !ring || ring.hasAttribute('hidden') || getComputedStyle(ring).display === 'none';
            const ringedNode = !ringHidden
                ? [...document.querySelectorAll('[data-journey-node]')].find(
                      (node) => node.getAttribute('cx') === ring.getAttribute('cx') && node.getAttribute('cy') === ring.getAttribute('cy'))
                : null;
            return {
                spanHidden: span.hidden,
                spanLeft: span.style.left,
                spanWidth: span.style.width,
                ribbonCount: currentRibbons.length,
                ribbonStart: currentRibbons.length ? currentRibbons[0].dataset.ribbonStart : null,
                rowPositions: currentRows.map((row) => row.dataset.trackPosition),
                ringHidden,
                ringNodeIndex: ringedNode ? ringedNode.dataset.nodeIndex : null,
                glyphHidden: glyphCursor.hidden,
                glyphLeft: glyphCursor.style.left,
                readout: document.querySelector('[data-timeline-readout]').textContent.trim(),
                tooltip: document.querySelector('[data-timeline-tooltip]').textContent.trim(),
                valueNow: document.querySelector('[data-timeline-inspector]').getAttribute('aria-valuenow'),
                valueText: document.querySelector('[data-timeline-inspector]').getAttribute('aria-valuetext'),
            };
        }"""
    )


async def test_one_elapsed_time_drives_every_inspection_target_from_pointer_glyph_and_keyboard(page: Any, seed: Seeder) -> None:
    """The bead's whole claim: one time, seven marks, three input routes, and a rest at the peak.

    Asserted as WHOLE STATES rather than one mark at a time. The failure this is built for is a
    partial update -- the readout moves and the wheel does not, or the row highlights and the
    glyph keeps the previous cell -- which every per-mark assertion in the world passes as long
    as it is looking at the mark that did update.
    """
    _file, timeline, rest_sec = await _open_inspectable_record(page, seed)
    inspector = timeline.locator("[data-timeline-inspector]")

    # --- 1. Nothing hovered: the page rests on the set's peak and says so ---------------------
    resting = await _marks(page)
    # The rest position IS the stored `set_profile.peak_sec` (phaze-0zx26), so it is compared
    # against that value rather than against a literal. `aria-valuenow` is written as
    # `toFixed(3)`, so the comparison rounds to the DOM's own precision -- NOT a tolerance on the
    # position, which would be the one loosening that lets this stop discriminating: a rest on
    # the raw-window argmax sits 2.86 s away and has to fail here.
    assert float(resting["valueNow"]) == round(rest_sec, 3)
    assert "peak" in resting["tooltip"].lower()
    # aria-valuetext names all four facts the tooltip shows, so a screen reader is not told less.
    # The remaining marks are stated INDEPENDENTLY, not re-derived from `rest_sec` -- a mark
    # computed the way the page computes it would agree with the page by construction.
    # 537.142857 s (arc sample 47 of 64) rounds to 537 s for the readout, i.e. 8:57.
    assert "At 8:57" in resting["valueText"]
    assert "track 2 Closing Track" in resting["valueText"]
    assert "key " in resting["valueText"] and "8B" in resting["valueText"]
    assert "mood " in resting["valueText"]
    # The peak sits inside track 2, in the fine window starting at 510 s, in the second key run,
    # and over the fifth of six glyph cells. Every mark agrees, because one time decided them.
    assert resting["spanHidden"] is False
    assert _percent(resting["spanLeft"]) == pytest.approx((_TRACK_TWO_START_SEC / _INSPECTION_DURATION_SEC) * 100, abs=0.01)
    assert resting["ribbonCount"] == 1
    assert float(resting["ribbonStart"]) == 510.0
    assert resting["rowPositions"] == ["2"]
    assert resting["ringHidden"] is False
    assert resting["ringNodeIndex"] == "1"
    assert resting["glyphHidden"] is False
    assert _percent(resting["glyphLeft"]) == pytest.approx((4 + 0.5) / 6 * 100, abs=0.01)

    # --- 2. The pointer over the lanes moves every one of them together ----------------------
    bounds = await inspector.bounding_box()
    assert bounds is not None
    await page.mouse.move(bounds["x"] + bounds["width"] * 0.1, bounds["y"] + 20)
    hovered = await _marks(page)
    assert float(hovered["valueNow"]) == pytest.approx(_INSPECTION_DURATION_SEC * 0.1, abs=2.0)
    assert "track 1 Opening Track" in hovered["valueText"]
    assert _percent(hovered["spanLeft"]) == 0.0
    assert hovered["rowPositions"] == ["1"]
    assert hovered["ringNodeIndex"] == "0"
    assert _percent(hovered["glyphLeft"]) == pytest.approx(0.5 / 6 * 100, abs=0.01)
    assert float(hovered["ribbonStart"]) == 60.0
    assert "peak" not in hovered["tooltip"].lower()

    # --- 3. The glyph is a pointer surface too, indexed by CELL, not by elapsed fraction ------
    glyph = page.locator("[data-set-glyph]").first
    glyph_bounds = await glyph.bounding_box()
    assert glyph_bounds is not None
    await page.mouse.move(glyph_bounds["x"] + glyph_bounds["width"] * 0.95, glyph_bounds["y"] + glyph_bounds["height"] / 2)
    from_glyph = await _marks(page)
    # The sixth cell is coarse window [600, 720); the cursor lands on its midpoint, not on 95%
    # of the duration -- reading the glyph as a time axis is the bug this pins.
    assert from_glyph["valueNow"] == "660"
    assert _percent(from_glyph["glyphLeft"]) == pytest.approx((5 + 0.5) / 6 * 100, abs=0.01)

    # --- 4. Hovering a tracklist row scrubs to that track's midpoint --------------------------
    await page.locator('[data-track-row][data-track-position="1"]').hover()
    from_row = await _marks(page)
    assert float(from_row["valueNow"]) == _TRACK_ONE_MIDPOINT_SEC
    assert from_row["rowPositions"] == ["1"]
    assert from_row["ringNodeIndex"] == "0"

    # --- 5. Keyboard reaches the same states, and focusing a row is one of the routes ---------
    await inspector.focus()
    await page.keyboard.press("Home")
    await page.keyboard.press("ArrowRight")
    from_keys = await _marks(page)
    assert from_keys["valueNow"] == "15"
    assert from_keys["rowPositions"] == ["1"]
    assert from_keys["ribbonCount"] == 1

    await page.locator('[data-track-row][data-track-position="2"]').focus()
    from_focus = await _marks(page)
    assert float(from_focus["valueNow"]) == _TRACK_TWO_MIDPOINT_SEC
    assert from_focus["rowPositions"] == ["2"]
    assert from_focus["ringNodeIndex"] == "1"
    assert "track 2 Closing Track" in from_focus["valueText"]

    # --- 6. Leaving returns the whole page to the peak ---------------------------------------
    await page.mouse.move(bounds["x"] + bounds["width"] * 0.4, bounds["y"] + 20)
    assert (await _marks(page))["valueNow"] != resting["valueNow"]
    await page.mouse.move(4, 4)
    await page.wait_for_function(
        "(expected) => document.querySelector('[data-timeline-inspector]').getAttribute('aria-valuenow') === expected",
        arg=resting["valueNow"],
    )
    left = await _marks(page)
    assert "peak" in left["tooltip"].lower()
    assert left == resting


async def test_hovering_the_bpm_chart_shows_the_fine_window_bpm_and_a_marker_at_the_cursor(page: Any, seed: Seeder) -> None:
    """phaze-f8ptt: the tooltip headline states the BPM in words, and a dot on the chart shows it.

    ``analysis_windows`` seeds fine BPM as ``126.0 + (index % 5)`` over 24 30 s windows, so the
    distinct values are exactly {126, 127, 128, 129, 130} and ``rounded_bpm_bounds`` rounds that
    outward to [120, 130] -- both read from the fixture's own construction, not restated as
    literals that merely happen to match today. Window 0 ([0, 30)) carries BPM 126.0, so parking
    the cursor at time 0 makes both the tooltip text and the marker's vertical position exact:
    fraction-from-top = (130 - 126) / (130 - 120) = 0.4, i.e. 40%.
    """
    _file, timeline, _rest_sec = await _open_inspectable_record(page, seed)
    inspector = timeline.locator("[data-timeline-inspector]")
    tooltip = timeline.locator("[data-timeline-tooltip]")
    cursor = timeline.locator("[data-timeline-cursor]")
    marker = timeline.locator("[data-timeline-bpm-marker]")

    await inspector.focus()
    await page.keyboard.press("Home")

    tooltip_text = await tooltip.inner_text()
    assert tooltip_text.startswith("At 0:00 · BPM 126")

    assert await marker.is_visible()
    assert _percent(await marker.evaluate("el => el.style.left")) == pytest.approx(_percent(await cursor.evaluate("el => el.style.left")), abs=0.01)
    assert _percent(await marker.evaluate("el => el.style.top")) == pytest.approx(40.0, abs=0.5)
    label_text = await marker.locator("[data-timeline-bpm-marker-label]").inner_text()
    assert label_text == "BPM 126"

    # Hovering a different window moves the tooltip, cursor, and dot together -- the fixture's
    # window 2 ([60, 90)) carries BPM 128.0, and the dot's fraction moves with it.
    bounds = await inspector.bounding_box()
    assert bounds is not None
    await page.mouse.move(bounds["x"] + bounds["width"] * (75.0 / _INSPECTION_DURATION_SEC), bounds["y"] + 20)
    moved_text = await tooltip.inner_text()
    assert "BPM 128" in moved_text
    assert _percent(await marker.evaluate("el => el.style.top")) == pytest.approx(20.0, abs=0.5)


async def test_a_window_with_no_measured_bpm_reads_an_explicit_absence_not_undefined(page: Any, seed: Seeder) -> None:
    """phaze-f8ptt: an unmeasured BPM is stated plainly, and the chart marker simply does not light.

    Every fine window's ``bpm`` is cleared after seeding, which also empties the server's own
    ``bpm_lo``/``bpm_hi`` scale (``bpm_spark`` returns ``None, None`` with no valid BPM anywhere) --
    so this exercises both halves of the absence at once: the tooltip headline's explicit "BPM —"
    and the marker having no scale to place a dot on.
    """
    file = await seed.file(filename="<set-01>.mp3")
    await seed.metadata(file, duration=720.0)
    windows = await seed.analysis_windows(file, fine_count=24, coarse_count=6)
    async with seed.session.begin_nested():
        for window in windows:
            if window.tier == "fine":
                window.bpm = None
    await seed.session.commit()

    await page.goto(f"/files/{file.id}", wait_until="domcontentloaded")
    timeline = page.locator("[data-analysis-timeline]")
    await timeline.wait_for(state="visible")
    await page.wait_for_function("() => document.querySelector('[data-analysis-timeline]').dataset.timelineReady === 'true'")
    inspector = timeline.locator("[data-timeline-inspector]")
    tooltip = timeline.locator("[data-timeline-tooltip]")
    marker = timeline.locator("[data-timeline-bpm-marker]")

    await inspector.focus()
    await page.keyboard.press("Home")

    tooltip_text = await tooltip.inner_text()
    assert "BPM —" in tooltip_text
    assert "undefined" not in tooltip_text.lower()
    assert await marker.is_hidden()

    # No BPM value anywhere means no scale either -- the server renders no `data-bpm-lo`/
    # `data-bpm-hi`, which is the other half of the same absence.
    bpm_plot = timeline.locator("[data-timeline-bpm-plot]")
    assert await bpm_plot.get_attribute("data-bpm-lo") is None
    assert await bpm_plot.get_attribute("data-bpm-hi") is None


async def test_hovering_or_focusing_a_wheel_node_drives_the_timeline_to_that_key_run(page: Any, seed: Seeder) -> None:
    """phaze-n0h86: the wheel is an input route too, by pointer and by keyboard.

    The seeded fine tier is 12 windows of A minor (8A) then 12 of C major (8B), 30 s each, so the
    two key runs are [0, 360) and [360, 720) and their midpoints -- where a node lands the cursor --
    are 180 s and 540 s. Stated here from the fixture, not read back from the page's payload, so
    the page cannot agree with this test merely by agreeing with itself.

    Each state is checked on BOTH sides of the link: the timeline's value and readout moved to the
    node's run, and the ring (visibility read from the attribute and computed style, see
    ``_marks``) sits on the node that was hovered. Keyboard reach is proved with a real Tab press
    from one node to the next, not a scripted ``focus()`` alone, because a node that is focusable
    only by script is not reachable by anyone using a keyboard.
    """
    _file, _timeline, _rest_sec = await _open_inspectable_record(page, seed)
    resting = await _marks(page)
    node_zero = page.locator('[data-journey-node][data-node-index="0"]')
    node_one = page.locator('[data-journey-node][data-node-index="1"]')

    # --- Pointer: each node scrubs the timeline to its own run --------------------------------
    await node_zero.hover()
    from_zero = await _marks(page)
    assert from_zero["valueNow"] == "180"
    assert "8A" in from_zero["valueText"]
    assert from_zero["rowPositions"] == ["1"]
    assert from_zero["ringHidden"] is False
    assert from_zero["ringNodeIndex"] == "0"

    await node_one.hover()
    from_one = await _marks(page)
    assert from_one["valueNow"] == "540"
    assert "8B" in from_one["valueText"]
    assert from_one["rowPositions"] == ["2"]
    assert from_one["ringNodeIndex"] == "1"

    # Leaving the wheel returns the whole page to the peak, exactly as leaving the lanes does.
    await page.mouse.move(4, 4)
    await page.wait_for_function(
        "(expected) => document.querySelector('[data-timeline-inspector]').getAttribute('aria-valuenow') === expected",
        arg=resting["valueNow"],
    )
    assert await _marks(page) == resting

    # --- Keyboard: nodes are tab stops, and Tab between them drives the timeline --------------
    await node_zero.focus()
    assert (await _marks(page))["valueNow"] == "180"
    await page.keyboard.press("Tab")
    assert await node_one.evaluate("el => el === document.activeElement")
    from_tab = await _marks(page)
    assert from_tab["valueNow"] == "540"
    assert from_tab["ringNodeIndex"] == "1"

    # Tabbing OUT of the wheel rests the page, like tabbing out of the tracklist.
    await page.keyboard.press("Tab")
    assert not await page.evaluate("() => document.querySelector('[data-harmonic-wheel]').contains(document.activeElement)")
    await page.wait_for_function(
        "(expected) => document.querySelector('[data-timeline-inspector]').getAttribute('aria-valuenow') === expected",
        arg=resting["valueNow"],
    )

    # The ring is decoration: hidden from the accessibility tree, and no live region was added
    # anywhere for the wheel to announce through.
    ring = page.locator("[data-journey-cursor-ring]")
    assert await ring.get_attribute("aria-hidden") == "true"
    assert await page.locator("[data-harmonic-journey] [aria-live]").count() == 0


async def test_hovering_a_runs_table_row_drives_the_timeline_and_rings_the_same_wheel_node(page: Any, seed: Seeder) -> None:
    """phaze-37ovq: the runs table is one more route to the same one elapsed time.

    Same fixture and same two runs (`_open_inspectable_record`) as the wheel-node test above, but
    driven through `[data-journey-row]` instead of `[data-journey-node]` -- both carry the
    IDENTICAL `data-node-index`, so hovering a row must scrub the timeline to the same run AND
    ring the same wheel node hovering its node would, through the shared `nodeTime` lookup.

    Pointer only, deliberately: a row carries no `tabindex` (see the template's own comment) --
    the wheel's nodes are already the keyboard route to every run, and giving rows a second,
    redundant set of tab stops right after them would break phaze-n0h86's own "tabbing out of the
    wheel rests the page" contract, asserted in the test just above this one.
    """
    _file, _timeline, _rest_sec = await _open_inspectable_record(page, seed)
    resting = await _marks(page)
    row_zero = page.locator('[data-journey-row][data-node-index="0"]')
    row_one = page.locator('[data-journey-row][data-node-index="1"]')

    assert await row_zero.get_attribute("tabindex") is None
    assert await row_one.get_attribute("tabindex") is None

    await row_zero.hover()
    from_zero = await _marks(page)
    assert from_zero["valueNow"] == "180"
    assert "8A" in from_zero["valueText"]
    assert from_zero["ringHidden"] is False
    assert from_zero["ringNodeIndex"] == "0"

    await row_one.hover()
    from_one = await _marks(page)
    assert from_one["valueNow"] == "540"
    assert "8B" in from_one["valueText"]
    assert from_one["ringNodeIndex"] == "1"

    # Leaving the table returns the whole page to the peak, exactly as leaving the wheel does.
    await page.mouse.move(4, 4)
    await page.wait_for_function(
        "(expected) => document.querySelector('[data-timeline-inspector]').getAttribute('aria-valuenow') === expected",
        arg=resting["valueNow"],
    )
    assert await _marks(page) == resting


async def test_a_time_in_a_coarse_gap_marks_no_glyph_cell_rather_than_the_nearest_one(page: Any, seed: Seeder) -> None:
    """A file whose coarse coverage stops early leaves the glyph unmarked past its last cell.

    The glyph's horizontal axis is the coarse window ORDINAL, so a time with no coarse window
    has no cell. Marking the nearest one would put the cursor on a window the reader is not
    pointing at, and it would look entirely correct while doing it.
    """
    file = await seed.file(filename="<set-01>.mp3")
    await seed.metadata(file, duration=720.0)
    # 24 fine windows spanning 720 s, but coarse coverage that stops at 240 s: everything after
    # that is a real hole in the coarse tier, which is where the glyph's cells come from.
    windows = await seed.analysis_windows(file, fine_count=24, coarse_count=6, projected=True)
    async with seed.session.begin_nested():
        for window in windows:
            if window.tier == "coarse" and window.start_sec >= 240.0:
                await seed.session.delete(window)
    await seed.session.commit()
    remaining = [w for w in windows if w.tier == "fine" or w.start_sec < 240.0]
    await seed.set_profile(file, remaining)

    await page.goto(f"/files/{file.id}", wait_until="domcontentloaded")
    await page.wait_for_function("() => document.querySelector('[data-analysis-timeline]').dataset.timelineReady === 'true'")
    inspector = page.locator("[data-timeline-inspector]")

    await inspector.focus()
    await page.keyboard.press("End")
    marks = await _marks(page)

    assert marks["valueNow"] == "720"
    assert marks["glyphHidden"] is True
    # The fine tier still covers the file's end, so the key ribbon and the wheel ring do light:
    # the glyph going dark is about the COARSE hole, not about the cursor being off the file.
    assert marks["ribbonCount"] == 1
    assert marks["ringHidden"] is False


async def test_a_burst_of_pointer_samples_in_one_frame_updates_the_page_once_and_lands_on_the_last(page: Any, seed: Seeder) -> None:
    """PR #556 review, finding 7: pointer motion was unthrottled, and each sample walked the DOM.

    Every `pointermove` re-marked the ribbons, every tracklist row and the glyph cursor, and
    every sample past the first in an animation frame is overwritten before anything is painted.
    The samples here are dispatched from ONE synchronous loop, so they are guaranteed to fall in
    a single frame -- that is what makes this deterministic rather than timing-dependent.

    Two claims, and the second is what keeps the first from being satisfied by simply dropping
    work: the readout is written a small number of times, AND the position it ends on is the
    LAST sample's, not the first's. A leading-edge-only throttle passes the count and fails the
    position; the shipped one runs the first sample synchronously and the last on the next frame.
    """
    _file, _timeline, _rest_sec = await _open_inspectable_record(page, seed)

    result = await page.evaluate(
        """async () => {
            const inspector = document.querySelector('[data-timeline-inspector]');
            const readout = document.querySelector('[data-timeline-readout]');
            const box = inspector.getBoundingClientRect();
            const samples = 40;
            let writes = 0;
            const observer = new MutationObserver((records) => { writes += records.length; });
            // Start on a fresh frame, so the burst below cannot be split across two of them.
            await new Promise((resolve) => requestAnimationFrame(() => resolve()));
            observer.observe(readout, {childList: true, characterData: true, subtree: true});
            for (let i = 1; i <= samples; i += 1) {
                inspector.dispatchEvent(new PointerEvent('pointermove', {
                    clientX: box.left + (box.width * i) / (samples + 1),
                    clientY: box.top + 20,
                    bubbles: true,
                }));
            }
            // A MutationObserver delivers its records at a MICROTASK checkpoint, and the loop
            // above is one synchronous block -- reading `writes` without yielding first reports
            // 0 no matter what the page did.
            await Promise.resolve();
            const duringBurst = writes;
            await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
            observer.disconnect();
            return {
                samples,
                duringBurst,
                writes,
                valueNow: Number(inspector.getAttribute('aria-valuenow')),
                lastSampleFraction: samples / (samples + 1),
            };
        }"""
    )

    assert result["samples"] == 40
    # One synchronous leading sample plus one deferred trailing flush. Bounded generously so a
    # browser that splits the microtask differently does not flake, and far below 40 either way.
    assert result["writes"] <= 5, f"{result['writes']} readout writes for {result['samples']} samples in one frame"
    assert result["duringBurst"] >= 1, "the first sample of a frame must apply synchronously, not a frame later"
    assert result["valueNow"] == pytest.approx(_INSPECTION_DURATION_SEC * result["lastSampleFraction"], abs=2.0)


async def test_the_tracklist_rows_are_re_resolved_after_an_htmx_swap_replaces_them(page: Any, seed: Seeder) -> None:
    """The cached row list is invalidated by `htmx:afterSwap`, so a swapped row still highlights.

    Caching the rows is the other half of finding 7, and it is the half that can break the page:
    htmx replaces the tracklist's inner container while the listeners stay delegated on the
    section outside it, so rows captured once would be detached nodes after the first Prioritize
    or Refresh and would silently stop following the cursor. Asserted by REPLACING the rows and
    then inspecting -- the cache is invisible, its failure mode is not.
    """
    _file, timeline, _rest_sec = await _open_inspectable_record(page, seed)
    inspector = timeline.locator("[data-timeline-inspector]")

    # Warm the cache: hover once so the row list is resolved and held.
    bounds = await inspector.bounding_box()
    assert bounds is not None
    await page.mouse.move(bounds["x"] + bounds["width"] * 0.1, bounds["y"] + 20)
    assert (await _marks(page))["rowPositions"] == ["1"]

    # Replace every row node with a fresh clone and announce it exactly as htmx would.
    await page.evaluate(
        """() => {
            const tracklist = document.querySelector('[data-tracklist-index]');
            const container = tracklist.querySelector('[data-track-row]').closest('tbody');
            container.replaceChildren(...[...container.children].map((row) => row.cloneNode(true)));
            container.dispatchEvent(new CustomEvent('htmx:afterSwap', {bubbles: true, detail: {target: container}}));
        }"""
    )

    await page.mouse.move(bounds["x"] + bounds["width"] * 0.9, bounds["y"] + 20)
    after_swap = await _marks(page)

    assert after_swap["rowPositions"] == ["2"], "a row that arrived from a swap must still follow the cursor"
