"""phaze-tsbdw: the shared table-header style and the sortable-header focus ring, as a real browser paints them.

``tests/shared/test_table_header_guard.py`` pins the markup (every ``<th>`` carries ``table-th``); it cannot
tell whether the class actually computes to the same type treatment everywhere or whether the ring paints,
because the compiled stylesheet is what decides both. This drives Chromium against the compiled ``app.css``
across the pages that used to carry the three variants.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser import seed as _seed


pytestmark = pytest.mark.browser

# Workspaces that render a column header: the Jura variant (files, metadata, analyze) and the former Inter-bold
# variant (discover's Recent scans, agents).
_WORKSPACES = ("files", "discover", "metadata", "analyze", "agents", "audit")

_PROBE_JS = """
() => [...document.querySelectorAll('th[scope="col"]')]
    .filter((th) => th.checkVisibility() && th.textContent.trim())
    .map((th) => {
        const s = getComputedStyle(th);
        return {
            text: th.textContent.trim().slice(0, 24),
            family: s.fontFamily.split(',')[0].replace(/["']/g, '').trim(),
            size: s.fontSize,
            transform: s.textTransform,
            spacing: s.letterSpacing,
        };
    })
"""


async def _open(page: Any, path: str) -> None:
    await page.goto(path, wait_until="domcontentloaded")
    await page.wait_for_selector("#stage-workspace", state="attached")
    await page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined")
    await page.wait_for_timeout(800)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_every_visible_column_header_computes_the_same_type_treatment(theme: str, page_at: Any, browser_dsn: str) -> None:
    await _seed.reset_dsn(browser_dsn)
    try:
        await _seed.seed_populated(browser_dsn)
        async with page_at(viewport="desktop", theme=theme) as page:
            seen: dict[str, int] = {}
            for workspace in _WORKSPACES:
                await _open(page, f"/s/{workspace}")
                for header in await page.evaluate(_PROBE_JS):
                    treatment = (header["family"], header["size"], header["transform"], header["spacing"])
                    seen[str(treatment)] = seen.get(str(treatment), 0) + 1
                    assert header["family"] == "Jura", f"/s/{workspace} {header['text']!r}: {header}"
                    assert header["size"] == "11px", f"/s/{workspace} {header['text']!r}: {header}"
                    assert header["transform"] == "uppercase", f"/s/{workspace} {header['text']!r}: {header}"
                    assert header["spacing"] not in {"normal", "0px"}, f"/s/{workspace} {header['text']!r}: {header}"
            assert len(seen) == 1, f"more than one header treatment rendered: {seen}"
            assert sum(seen.values()) > 10, "the probe found too few headers to mean anything"
    finally:
        await _seed.reset_dsn(browser_dsn)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_a_focused_sortable_header_paints_the_standard_ring(theme: str, page_at: Any, browser_dsn: str) -> None:
    await _seed.reset_dsn(browser_dsn)
    try:
        await _seed.seed_populated(browser_dsn)
        async with page_at(viewport="desktop", theme=theme) as page:
            await _open(page, "/s/files")
            button = page.locator("th button.table-th-sort").first
            await button.wait_for(state="visible")
            # :focus-visible follows the input modality, so a key press first makes the programmatic focus a keyboard one.
            await page.keyboard.press("Shift")
            await button.focus()
            ring = await button.evaluate(
                "(el) => ({ shadow: getComputedStyle(el).boxShadow, outline: getComputedStyle(el).outlineStyle, visible: el.matches(':focus-visible') })"
            )
            assert ring["visible"], ring
            assert ring["shadow"] != "none", f"no ring painted on a focused sortable header: {ring}"
            assert ring["outline"] in {"none", "hidden"}, f"browser default outline instead of the ring: {ring}"
            # The ring colour is the app's blue-500 (the same token the pager buttons and filter selects use).
            probe = await page.evaluate(
                """() => { const d = document.createElement('div'); d.className = 'ring-2 ring-blue-500'; document.body.appendChild(d);
                const c = getComputedStyle(d).getPropertyValue('--tw-ring-color'); d.remove(); return c; }"""
            )
            assert probe, "ring-blue-500 not compiled"
    finally:
        await _seed.reset_dsn(browser_dsn)
