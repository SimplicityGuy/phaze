"""An operator can follow failure attention and retain its population through real HTMX controls."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from phaze.models.metadata import FileMetadata
from tests.browser.helpers import click_swap, open_shell, settled, swap_settles


if TYPE_CHECKING:
    from playwright.async_api import Page

    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser


async def test_failure_attention_and_metadata_inspection(page: Page, seed: Seeder) -> None:
    failed = await seed.file(filename="Synthetic resolved failure.mp3")
    keeper = await seed.file(filename="Synthetic keeper.mp3")
    companion = await seed.file(filename="Synthetic companion.nfo", file_type="nfo")
    missing = await seed.file(filename="Synthetic missing.mp3")
    missing.missing_at = datetime.now(UTC)
    await seed.metadata(keeper)
    seed.session.add_all([FileMetadata(file_id=file.id, failed_at=datetime.now(UTC)) for file in (failed, companion, missing)])
    await seed.session.commit()
    await seed.resolve_duplicate(keeper=keeper, archived=[failed])

    await open_shell(page)
    await settled(page)
    link = page.locator("#stage-workspace a").filter(has_text="Open Files").first
    assert await link.get_attribute("href") == "/s/files?stage=metadata&bucket=failed&scope=media"
    await link.click()
    await page.wait_for_url("**/s/files?stage=metadata&bucket=failed&scope=media")
    await settled(page)
    assert await page.locator("#files-table-view").get_by_text(failed.original_filename, exact=False).count() > 0
    assert await page.locator("#files-table-view").get_by_text(companion.original_filename, exact=False).count() == 0
    assert await page.locator("#files-table-view").get_by_text(missing.original_filename, exact=False).count() == 0
    await click_swap(page, '#files-table-view th [hx-get*="sort=file"]')
    assert "scope=media" in page.url
    assert await page.locator('input[name="scope"][value="media"]').count() > 0
    async with swap_settles(page):
        await page.get_by_label("Filter by status").select_option("done")
    assert "scope=media" in page.url and "bucket=done" in page.url
    assert await page.locator("#files-table-view").get_by_text(keeper.original_filename, exact=False).count() > 0

    await page.set_viewport_size({"width": 390, "height": 844})
    await open_shell(page, "/s/metadata")
    await settled(page)
    assert await page.locator("#metadata-eligible-value").inner_text() == "0"
    inspection = page.get_by_role("link", name="Inspect failed files", exact=True)
    assert await inspection.is_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    Path("test-results/screenshots").mkdir(parents=True, exist_ok=True)
    await page.screenshot(path="test-results/screenshots/metadata-failure-inspection-390.png", full_page=True)
    await inspection.click()
    await page.wait_for_url("**/s/files?stage=metadata&bucket=failed&scope=media")
    await settled(page)
    assert await page.get_by_text("Music and video", exact=True).is_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.screenshot(path="test-results/screenshots/metadata-failed-files-390.png", full_page=True)
    await click_swap(page, '#files-table-view a:has-text("Include all file types")')
    assert "scope=media" not in page.url
    assert await page.locator("#files-table-view").get_by_text(companion.original_filename, exact=False).count() > 0
