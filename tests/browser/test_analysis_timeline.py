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

    await viewport.evaluate("el => { el.scrollLeft = el.scrollWidth; el.dispatchEvent(new Event('scroll')); }")
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
