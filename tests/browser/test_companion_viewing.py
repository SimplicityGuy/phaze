"""Real HTMX keyboard and narrow-view companion page/drawer browsing."""

import pytest

from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.local_source_import import import_local_source
from tests.browser.helpers import open_shell, swap_settles
from tests.browser.test_files_record import _details_button, _wait_for_record
from tests.integration.test_companion_viewing import TEXT
from tests.integration.test_local_source_import import inventory


pytestmark = pytest.mark.browser


@pytest.mark.parametrize("kind", ["mp3", "mp4"])
async def test_companion_both_kinds_keyboard_lazy_text_page_drawer_narrow(page, seed, kind):
    await seed.agent("test-fileserver")
    media, _companion, raw = await inventory(seed.session, TEXT, "nfo")
    media.file_type = kind
    await import_local_source(seed.session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await seed.session.commit()
    await open_shell(page, f"/files/{media.id}")
    assert await page.locator("[data-visible-track]").first.is_visible()
    assert "Example Festival" in await page.locator("[data-release-metadata]").inner_text()
    page_text = await page.locator("[data-record-content]").inner_text()
    track = page.get_by_role("button", name="Read all track rows, text and review").first
    await track.focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await page.locator("[data-stored-observation]").first.wait_for()
    text = page.get_by_role("button", name="Read original companion text").first
    await text.focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await page.locator("[data-companion-text]").first.wait_for()
    assert '<script>alert("synthetic")</script>' in await page.locator("[data-companion-text]").first.inner_text()
    assert await page.locator("[data-companion-text] script").count() == 0
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.locator("[data-visible-release-field]").first.is_visible()
    overflow = await page.evaluate("""() => {
        const width = document.documentElement.clientWidth;
        return [...document.querySelectorAll('[data-record-content] *')]
            .filter(el => el.getBoundingClientRect().right > width + 1)
            .map(el => ({tag: el.tagName, classes: el.className,
                text: el.textContent.slice(0, 120), right: el.getBoundingClientRect().right,
                whiteSpace: getComputedStyle(el).whiteSpace})).slice(0, 12);
    }""")
    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), overflow
    await page.set_viewport_size({"width": 1280, "height": 900})
    await open_shell(page, "/s/files")
    await page.locator(_details_button(media.id)).click()
    await _wait_for_record(page)
    drawer_text = await page.locator("#record-body [data-record-content]").inner_text()
    assert page_text == drawer_text
    assert await page.locator("#record-body [data-visible-release-field]").first.is_visible()


async def test_native_form_reimport_select_and_html_error(page, seed):
    await seed.agent("test-fileserver")
    media, companion, raw = await inventory(seed.session)
    await import_local_source(seed.session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await seed.session.commit()
    await open_shell(page, f"/files/{media.id}")
    await page.get_by_role("button", name="Read all track rows, text and review").click()
    await page.locator("[data-source-decision]").wait_for()
    await page.get_by_role("button", name="Select this tracklist", exact=True).click()
    await page.get_by_role("status").filter(has_text="Source selected.").wait_for()
    await page.get_by_role("button", name="Reviewed tracklist", exact=True).click()
    await page.locator("[data-reviewed-source]").filter(has_text="Selected tracklist").wait_for()
    await page.get_by_role("button", name="Reimport stored text", exact=True).click()
    await page.get_by_role("status").filter(has_text="Stored text imported").wait_for()
    # A reviewed source revision changes while the original form remains open.
    companion.sha256_hash = "b" * 64
    await seed.session.commit()
    await page.get_by_role("button", name="Select this tracklist", exact=True).click()
    await page.get_by_role("alert").filter(has_text="Inventory revision changed before decision").wait_for()
