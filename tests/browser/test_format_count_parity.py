"""phaze-dwevc: the JS count formatter and the Jinja ``thousands`` filter agree, and live bindings stay formatted.

``static/js/format_count.js`` is executed in Chromium, because that is the artifact's real consumer -- this repo has no node
and no jsdom, and a Python port of a regex grouping would say nothing about the one the browser runs. The case table is
the SAME list ``tests/shared/ui/test_display_format.py`` runs through ``phaze.utils.humanize.format_count``, so a case added
there is checked against the JS automatically.

The second half proves the property the operator sees: write a bare integer into the Alpine store (what every poll does) and
the sidebar, the header status strip and the Summary tiles show it WITH separators, because their ``x-text`` bindings call
``formatCount``. A binding that rendered the raw store value would show ``145057`` here.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from tests.shared.ui.test_display_format import COUNT_CASES


pytestmark = pytest.mark.browser


async def _open(page: Any, path: str) -> None:
    await page.goto(path, wait_until="domcontentloaded")
    await page.wait_for_selector("#stage-workspace", state="attached")
    await page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined && typeof window.formatCount === 'function'")
    await page.wait_for_timeout(500)


def _label(value: object) -> str:
    return "nan" if isinstance(value, float) and math.isnan(value) else repr(value)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(("value", "expected"), COUNT_CASES, ids=[_label(case[0]) for case in COUNT_CASES])
async def test_the_js_formatter_matches_the_python_filter_on_every_case(value: object, expected: str, page: Any) -> None:
    await _open(page, "/s/summary")

    assert await page.evaluate("(v) => window.formatCount(v)", value) == expected


@pytest.mark.asyncio(loop_scope="session")
async def test_live_store_counts_keep_their_separators_after_every_update(page: Any) -> None:
    await _open(page, "/s/summary")

    for discovered in (145057, 145058, 1000000):
        await page.evaluate(
            """(n) => {
                const s = Alpine.store('pipeline');
                s.seedKnown = true;
                s.discovered = n;
                s.analyzeDone = 94772; s.analyzeTotal = 103338;
                s.tracklistDone = 12345;
                s.proposalsDone = 1000; s.proposalsTotal = 54321;
                s.metadataStatusKnown = true; s.metadataStatusDone = 104156; s.metadataStatusTotal = n;
                s.agentOnline = 1200; s.computeLanesActive = 3400;
            }""",
            discovered,
        )
        await page.wait_for_timeout(150)
        rail = await page.locator('[data-rail-stage="discover"]').text_content()
        expected = f"{discovered:,}"
        assert expected in (rail or ""), f"sidebar shows {rail!r}, expected {expected}"
        assert str(discovered) not in (rail or "").replace(expected, ""), "the raw, unformatted count is also on the page"

    analyze = await page.locator('[data-rail-stage="analyze"]').text_content()
    assert "94,772 / 103,338" in (analyze or "")
    proposals = await page.locator('[data-rail-stage="propose"]').text_content()
    assert "1,000 / 54,321" in (proposals or "")
    header = await page.locator("header").first.text_content()
    assert "1,200" in (header or "")
    assert "3,400" in (header or "")


@pytest.mark.asyncio(loop_scope="session")
async def test_summary_recent_activity_tiles_stay_formatted(page: Any) -> None:
    await _open(page, "/s/summary")

    await page.evaluate(
        "() => { const s = Alpine.store('pipeline'); s.summaryRecentLive = 1234; s.summaryRecentToday = 56789; s.summaryRecentLifetime = 7654321; }"
    )
    await page.wait_for_timeout(150)
    body = await page.locator("#stage-workspace").text_content()

    for expected in ("1,234", "56,789", "7,654,321"):
        assert expected in (body or ""), expected
