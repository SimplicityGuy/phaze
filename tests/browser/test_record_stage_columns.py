"""Stage/status alignment is a rendered-layout contract shared by the full page and record pane."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from phaze.models.scheduling_ledger import SchedulingLedger
from tests.browser.helpers import open_shell, settled, swap_settles


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("surface", ["page", "pane"])
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_stage_status_columns_align_without_overflow(open_page: Any, seed: Any, width: int, surface: str, theme: str) -> None:
    file = await seed.file(filename="<track-01>.mp3")
    await seed.metadata(file)
    seed.session.add(SchedulingLedger(key=f"process_file:{file.id}", function="process_file", routing="agent", payload={"file_id": str(file.id)}))
    await seed.session.commit()

    page = await open_page(theme=theme)
    await page.set_viewport_size({"width": width, "height": 1000})
    if surface == "page":
        await page.goto(f"/files/{file.id}", wait_until="domcontentloaded")
    else:
        await open_shell(page, "/s/files")
        await settled(page)
        await page.locator(f'button[hx-get="/record/{file.id}"]:visible').click()

    panel = page.locator(f"#record-stage-eligibility-{file.id}")
    await panel.wait_for(state="visible")
    await page.evaluate("document.fonts.ready")
    labels = panel.locator('button[aria-label^="Show "]')
    pills = panel.locator('[id^="stage-pill-"] > span')
    assert [text.strip() for text in await labels.all_text_contents()] == ["Metadata", "Analyze", "Tracklist", "Propose", "Review", "Execute"]
    assert await pills.count() == 6
    assert await pills.nth(0).get_attribute("aria-label") == "Metadata: done"
    assert await pills.nth(1).get_attribute("aria-label") == "Analyze: in flight"
    assert await pills.nth(2).get_attribute("aria-label") == "Tracklist: no stored tracklist"

    async def assert_columns() -> None:
        label_boxes = [await labels.nth(i).bounding_box() for i in range(6)]
        pill_boxes = [await pills.nth(i).bounding_box() for i in range(6)]
        assert max(box["x"] for box in label_boxes) - min(box["x"] for box in label_boxes) < 1
        assert max(box["x"] for box in pill_boxes) - min(box["x"] for box in pill_boxes) < 1
        assert abs(pill_boxes[0]["x"] - max(box["x"] + box["width"] for box in label_boxes) - 8) < 1
        assert len({round(box["width"]) for box in pill_boxes}) > 2, "status pills should keep their natural widths"
        bounds = await panel.bounding_box()
        for label, pill in zip(label_boxes, pill_boxes, strict=True):
            assert abs(label["y"] - pill["y"]) < 2, "a label must remain paired with its status when text wraps"
            assert pill["x"] + pill["width"] <= bounds["x"] + bounds["width"] + 1
        assert await panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

    await assert_columns()
    artifacts = Path("test-results/eligibility-columns")
    artifacts.mkdir(parents=True, exist_ok=True)
    await panel.screenshot(path=str(artifacts / f"{surface}-{width}-{theme}.png"))
    # Traces still swap beneath the chosen row without changing the two column tracks.
    async with swap_settles(page):
        await labels.nth(0).click()
    assert await panel.locator(f"#trace-metadata-{file.id}").inner_text()
    await assert_columns()
