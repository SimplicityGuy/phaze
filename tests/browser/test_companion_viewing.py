"""Real HTMX keyboard and narrow-view companion page/drawer browsing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.models.file_companion import FileCompanion
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.local_source_import import import_local_source
from tests.browser.helpers import open_shell, swap_settles
from tests.browser.test_files_record import _details_button, _wait_for_record
from tests.integration.test_companion_viewing import TEXT
from tests.integration.test_local_source_import import inventory


if TYPE_CHECKING:
    from playwright.async_api import Page

    from tests.browser.seed import Seeder


pytestmark = pytest.mark.browser


async def test_hostile_native_discogs_form_refuses_valid_pin_then_same_origin_review_succeeds(page, seed, live_server):
    """A genuine browser supplies the hostile Origin; retained pins do not authorize CSRF."""
    from sqlalchemy import select

    from phaze.models.discogs_link import DiscogsLink
    from tests.integration.test_selected_discogs_consumers import selected_inventory

    await seed.agent("test-fileserver")
    media, _companion, selected = await selected_inventory(seed.session)
    link = DiscogsLink(source_observation_id=selected.observation_id, source_track_position=1, discogs_release_id="synthetic", confidence=90)
    seed.session.add(link)
    await seed.session.commit()
    target = f"{live_server}/recording-discogs/{media.id}/links/{link.id}"
    hostile = live_server.replace("127.0.0.1", "localhost") + "/hostile"
    html = f'''<form method="post" action="{target}">
      <input name="observation_id" value="{selected.observation_id}">
      <input name="selected_token" value="{selected.selection_token}">
      <button name="action" value="accept">Hostile accept</button></form>'''
    await page.route(hostile, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    await page.goto(hostile)
    async with page.expect_response(lambda response: response.url == target and response.request.method == "POST") as received:
        await page.get_by_role("button", name="Hostile accept").click()
    assert (await received.value).status == 403
    assert await seed.session.scalar(select(DiscogsLink.status).where(DiscogsLink.id == link.id)) == "candidate"
    # Drive the real served native form, disabling only the optional HTMX enhancement.
    await page.goto(f"{live_server}/recording-discogs/{media.id}")
    await page.get_by_role("button", name="Accept", exact=True).evaluate("button => button.form.removeAttribute('hx-post')")
    async with page.expect_response(lambda response: response.url == target and response.request.method == "POST") as received:
        await page.get_by_role("button", name="Accept", exact=True).click()
    assert (await received.value).status == 200
    assert await seed.session.scalar(select(DiscogsLink.status).where(DiscogsLink.id == link.id)) == "accepted"


@pytest.mark.parametrize("kind", ["mp3", "mp4"])
async def test_companion_both_kinds_keyboard_lazy_text_page_drawer_narrow(page, seed, kind):
    await seed.agent("test-fileserver")
    media, _companion, raw = await inventory(seed.session, TEXT, "nfo" if kind == "mp3" else "txt")
    media.file_type = kind
    await import_local_source(seed.session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await seed.session.commit()
    await open_shell(page, f"/files/{media.id}")
    assert await page.locator("[data-visible-track]").first.is_visible()
    assert "Example Festival" in await page.locator("[data-release-metadata]").inner_text()
    page_text = await page.locator("[data-record-content]").inner_text()
    track = page.get_by_role("button", name="Review content").first
    await track.focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await page.locator("[data-stored-observation]").first.wait_for()
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
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.locator("#record-body [data-visible-release-field]").first.is_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


async def test_native_form_reimport_select_and_html_error(page, seed):
    await seed.agent("test-fileserver")
    media, companion, raw = await inventory(seed.session)
    await import_local_source(seed.session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await seed.session.commit()
    await open_shell(page, f"/files/{media.id}")
    await page.get_by_role("button", name="Review content").click()
    await page.locator("[data-source-decision]").first.wait_for()
    # A displayed candidate revision changes before the decision.
    companion.sha256_hash = "b" * 64
    await seed.session.commit()
    await page.get_by_role("button", name="Use this tracklist", exact=True).click()
    await page.get_by_role("alert").filter(has_text="Inventory revision changed before decision").wait_for()


async def test_workspace_links_keyboard_independent_release_and_tracklist_choices(page: Page, seed: Seeder) -> None:
    """Served controls are real; separate integration fixtures prove physical acquisition."""
    await seed.agent("test-fileserver")
    media, _first, raw = await inventory(seed.session, "Artist: First Artist\nAlbum: First Album\n01. First - Opening\n", "nfo")
    first = await import_local_source(seed.session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    media.original_path = "/synthetic/first-recording.mp3"
    await seed.session.flush()
    _other, second_source, second_raw = await inventory(
        seed.session, "Artist: Alternate Artist\nAlbum: Alternate Album\n01. Other - Alternate\n", "txt"
    )
    seed.session.add(FileCompanion(media_id=media.id, companion_id=second_source.id))
    await seed.session.flush()
    second = await import_local_source(seed.session, ImportLocalSource(media_id=media.id, raw_observation_id=second_raw))
    await seed.session.commit()
    await page.set_viewport_size({"width": 390, "height": 844})
    await open_shell(page, "/s/tracklist")
    await page.locator("#local-tracklist-sources table").wait_for()
    evidence = page.locator(f'a[href="/files/{media.id}#companion-sources"]').first
    await evidence.focus()
    await page.keyboard.press("Enter")
    await page.locator("[data-companion-overview]").wait_for()
    for observation, button in (
        (first.tracklist_observation_id, "Use this tracklist"),
        (second.release_observation_id, "Use this release info"),
    ):
        from phaze.models.provider_source import ProviderSourceObservation

        source_id = (await seed.session.get(ProviderSourceObservation, observation)).object_id
        card = page.locator(f'[data-source-object="{source_id}"]')
        await card.get_by_role("button", name="Review content", exact=True).click()
        form = page.locator(f'[data-stored-observation="{observation}"] [data-source-decision]')
        await form.wait_for()
        conflict = form.locator('input[name="accept_conflict"]')
        if await conflict.count():
            await conflict.check()
        await form.get_by_role("button", name=button, exact=True).click()
        kind = "tracklist" if button == "Use this tracklist" else "release_metadata"
        await page.locator(f'[data-current-source="{kind}"]').get_by_role("button").wait_for()
    await open_shell(page, f"/files/{media.id}")
    assert await page.locator('[data-current-source="tracklist"] [data-current-track]').count() > 0
    await page.get_by_role("button", name="View release info", exact=True).focus()
    async with swap_settles(page):
        await page.keyboard.press("Enter")
    await page.locator("[data-companion-text]").first.wait_for()
    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


async def test_guided_raw_extract_choose_and_pinned_reread(page, seed):
    """The user's actual workflow: one card, immediate text, extract, independent choices."""
    from phaze.models.provider_source import ProviderSourceObservation
    from phaze.services.companion_capture import store_capture_report
    from tests.integration.test_companion_import_backfill import report

    await seed.agent("test-fileserver")
    media, companion, raw = await inventory(
        seed.session, "Artist: Example Artist\nAlbum: Example Festival\n01. Example Artist - Opening\n02. Example Artist - Finale\n", "nfo"
    )
    source_id = (await seed.session.get(ProviderSourceObservation, raw)).object_id
    await seed.session.commit()
    await page.set_viewport_size({"width": 390, "height": 844})
    await open_shell(page, f"/files/{media.id}")
    overview = page.locator("[data-companion-overview]")
    await overview.get_by_role("button", name="Review content", exact=True).click()
    await overview.locator("[data-companion-text]").wait_for()
    assert "Example Festival" in await overview.locator("[data-companion-text]").inner_text()
    assert await overview.locator("[data-source-decision]").count() == 0
    await overview.get_by_role("button", name="Extract tracklist and release info", exact=True).click()
    await overview.get_by_role("button", name="Use this tracklist", exact=True).wait_for()
    assert "Example Festival" in await overview.locator("[data-companion-release-facts]").inner_text()
    await overview.get_by_role("button", name="Use this tracklist", exact=True).click()
    await overview.locator('[data-current-source="tracklist"] [data-current-track]').first.wait_for()
    assert "No release info chosen yet" in await overview.locator('[data-current-source="release_metadata"]').inner_text()
    await overview.get_by_role("button", name="Use this release info", exact=True).click()
    await overview.locator('[data-current-source="release_metadata"] [data-current-release-field]').first.wait_for()
    assert await overview.get_by_role("button", name="Use this tracklist", exact=True).count() == 0
    assert await page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    # A new capture must not replace the reviewed source or reuse the old extraction.
    companion.sha256_hash = "b" * 64
    await seed.session.flush()
    envelope = report(companion)
    envelope = envelope.model_copy(update={"read": envelope.read.model_copy(update={"text": "01. New Artist - New version\n"})})
    await store_capture_report(seed.session, companion.agent_id, envelope)
    await seed.session.commit()
    await open_shell(page, f"/files/{media.id}")
    await overview.get_by_role("button", name="Review content", exact=True).click()
    await overview.locator("[data-companion-text]").wait_for()
    assert "New version" in await overview.locator("[data-companion-text]").inner_text()
    assert "Opening" in await overview.locator('[data-current-source="tracklist"]').inner_text()
    assert await overview.locator("[data-source-decision]").count() == 0
    await overview.get_by_role("button", name="View tracklist", exact=True).click()
    pinned = overview.locator('[data-current-source="tracklist"] [data-companion-text]')
    await pinned.wait_for()
    assert "Opening" in await pinned.inner_text() and "New version" not in await pinned.inner_text()
    assert await overview.locator(f'[data-source-review="{source_id}"]').count() == 1
