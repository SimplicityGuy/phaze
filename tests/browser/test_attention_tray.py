"""Lazy nonmodal health tray keyboard behavior at every supported viewport and theme."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.test_shell_contract import _open


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("viewport", ["phone", "tablet", "desktop"])
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_health_tray_lazy_keyboard_focus_and_layout(page_at: Any, viewport: str, theme: str) -> None:
    async with page_at(viewport=viewport, theme=theme) as page:
        loads: list[str] = []
        page.on("request", lambda request: loads.append(request.url) if "/shell/attention" in request.url else None)
        await _open(page, "/s/files")
        assert loads == [], "attention items loaded before the tray was opened"
        assert await page.evaluate("document.documentElement.classList.contains('dark')") is (theme == "dark")
        if viewport == "phone":
            assert await page.evaluate("window.innerWidth") == 390
        assert await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1
        pill = page.locator("#attention-pill")
        assert await pill.get_attribute("aria-expanded") == "false"
        assert await pill.get_attribute("title") == await pill.get_attribute("aria-label")
        await pill.focus()
        await page.keyboard.press("Enter")
        tray = page.locator("#attention-tray")
        await tray.wait_for(state="visible")
        await page.wait_for_function("document.getElementById('attention-tray-content').textContent.indexOf('Loading attention items') === -1")
        assert len(loads) == 1
        assert await pill.get_attribute("aria-expanded") == "true"
        assert await tray.get_attribute("role") != "dialog"
        assert await tray.get_attribute("aria-modal") is None
        await page.wait_for_function("document.activeElement.id === 'attention-tray-heading'")
        assert await tray.evaluate("node => node.scrollWidth - node.clientWidth") <= 1
        bounds = await tray.bounding_box()
        assert bounds is not None and bounds["x"] >= 0
        assert bounds["x"] + bounds["width"] <= await page.evaluate("window.innerWidth")
        await page.keyboard.press("Tab")
        assert await page.evaluate("document.activeElement.getAttribute('aria-label')") == "Close attention tray"
        await page.keyboard.press("Escape")
        await tray.wait_for(state="hidden")
        await page.wait_for_function("document.activeElement.id === 'attention-pill'")
        assert await pill.get_attribute("aria-expanded") == "false"
        # A closed tray's global Escape handler must leave another control's focus alone.
        await page.locator("#cmdk-trigger").focus()
        await page.keyboard.press("Escape")
        assert await page.evaluate("document.activeElement.id") == "cmdk-trigger"
        await pill.focus()
        await page.keyboard.press("Enter")
        await tray.wait_for(state="visible")
        await page.wait_for_function("document.getElementById('attention-tray-content').textContent.indexOf('Loading attention items') === -1")
        await tray.get_by_role("link", name="Open Summary", exact=True).click()
        await tray.wait_for(state="hidden")
        await page.wait_for_function("document.activeElement.id === 'attention-pill'")
        assert "/s/summary" in page.url
