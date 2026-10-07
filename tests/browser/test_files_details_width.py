"""phaze-5gde8: the Files matrix Details column holds the Details button plus the cell padding, as Chromium lays it out."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser import seed as _seed


pytestmark = pytest.mark.browser

_CELL_PADDING_PX = 24

_PROBE_JS = """
() => {
    const btn = [...document.querySelectorAll('table button')].find((b) => b.checkVisibility() && b.textContent.trim().startsWith('Details'));
    const td = btn.closest('td');
    const th = [...document.querySelectorAll('th[scope="col"]')].find((h) => h.textContent.trim() === 'File details');
    const card = btn.closest('table').parentElement;
    const b = btn.getBoundingClientRect();
    const c = td.getBoundingClientRect();
    const k = card.getBoundingClientRect();
    return {btn: b.width, cell: c.width, th: th.getBoundingClientRect().width, right_gap: c.right - b.right, card_right_gap: k.right - b.right,
            scroll: card.scrollWidth - card.clientWidth};
}
"""


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [900, 1600])
async def test_details_button_sits_inside_its_cell_with_standard_padding(width: int, theme: str, page_at: Any, browser_dsn: str) -> None:
    await _seed.reset_dsn(browser_dsn)
    try:
        await _seed.seed_populated(browser_dsn)
        async with page_at(viewport={"viewport": {"width": width, "height": 900}}, theme=theme) as page:
            await page.goto("/s/files", wait_until="domcontentloaded")
            await page.wait_for_selector("#stage-workspace", state="attached")
            await page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined")
            await page.wait_for_timeout(800)
            m = await page.evaluate(_PROBE_JS)
            assert m["cell"] >= m["btn"] + _CELL_PADDING_PX, f"Details column {m['cell']}px cannot hold the {m['btn']}px button plus cell padding"
            assert m["right_gap"] >= _CELL_PADDING_PX / 2 - 0.5, f"button sits {m['right_gap']}px from its cell's right edge, expected px-3"
            if width == 900:
                assert m["scroll"] == 0, "at 900px the table must not overflow its card"
    finally:
        await _seed.reset_dsn(browser_dsn)
