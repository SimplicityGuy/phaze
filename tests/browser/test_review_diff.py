"""Shared review diffs and confidence remain readable and keyboard-operable in the browser."""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.helpers import open_shell


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_paired_review_rows_mark_changes_confidence_and_preserve_keyboard_editing(page_at: Any, seed: Any, viewport: str, theme: str) -> None:
    proposal = await seed.proposal(filename="old-song.mp3", proposed_filename="new-song.mp3", confidence=0.88)
    unknown = await seed.proposal(filename="unknown-song.mp3", confidence=None)
    tag_file = await seed.bulk_eligible_tag_change(filename="Example Artist - Example Track.mp3")
    async with page_at(viewport=viewport, theme=theme) as page:
        await open_shell(page, "/s/rename?status=all")
        assert await page.evaluate("document.documentElement.classList.contains('dark')") is (theme == "dark")
        if viewport == "phone":
            assert await page.evaluate("window.innerWidth") == 390
        assert await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1
        row = page.locator(f"#rename-row-{proposal.id}")
        old = row.locator('code[data-diff-side="before"]').first
        new = row.locator('code[data-diff-side="after"]').first
        await new.wait_for(state="visible")
        assert await old.inner_text() == "old-song.mp3"
        assert await new.inner_text() == "new-song.mp3"
        assert await old.locator("del[data-diff-changed]").inner_text() == "old"
        assert await new.locator("ins[data-diff-changed]").inner_text() == "new"
        assert "line-through" in await old.evaluate("node => getComputedStyle(node).textDecorationLine")
        assert "underline" in await new.locator("ins").evaluate("node => getComputedStyle(node).textDecorationLine")
        assert await row.locator('code[data-diff-side="after"]').count() == 2, "Destination must use the same marked diff"
        meter = row.get_by_role("meter", name="Confidence")
        assert await meter.get_attribute("value") == "88"
        assert await meter.get_attribute("aria-valuetext") == "88% confidence"
        bounds = await meter.bounding_box()
        assert bounds is not None and bounds["width"] > 0 and bounds["height"] > 0
        assert "Confidence: 88%" in await row.inner_text()
        unknown_row = page.locator(f"#rename-row-{unknown.id}")
        assert "Confidence: Unavailable" in await unknown_row.inner_text()
        assert await unknown_row.get_by_role("meter").count() == 0
        tag_row = page.locator(f"#tagwrite-row-{tag_file.id}")
        assert await tag_row.locator('code[data-diff-side="after"] ins[data-diff-changed]').count() > 0
        assert "Confidence: Unavailable" in await tag_row.inner_text()
        tag_token = await tag_row.get_by_role("button", name="APPROVE", exact=True).get_attribute("hx-vals")
        approve = row.get_by_role("button", name="APPROVE", exact=True)
        token = await approve.get_attribute("hx-vals")
        await approve.focus()
        await page.keyboard.press("Tab")
        edit = row.get_by_role("button", name="EDIT", exact=True)
        assert await edit.evaluate("node => node === document.activeElement")
        await page.keyboard.press("Enter")
        field = row.locator('input[name="proposed"]')
        await field.wait_for(state="visible")
        await page.wait_for_function(
            "selector => document.querySelector(selector) === document.activeElement", arg=f'#rename-row-{proposal.id} input[name="proposed"]'
        )
        await field.fill("unsaved-song.mp3")
        discard = row.get_by_role("button", name="DISCARD", exact=True)
        await discard.focus()
        await page.keyboard.press("Enter")
        await new.wait_for(state="visible")
        assert await new.inner_text() == "new-song.mp3"
        assert await approve.get_attribute("hx-vals") == token
        assert await tag_row.get_by_role("button", name="APPROVE", exact=True).get_attribute("hx-vals") == tag_token
        assert await unknown_row.locator("input[name=proposed]").count() == 0
