"""phaze-8p1uq: the Changes Review golden path, driven against seeded proposals.

``phaze-tzy6s.14`` could only reach this workspace's empty state, so "proposal review/approve" and
"search/tab state" were both covered by a smoke render that asserted the page did not throw. What
follows are the interaction paths themselves: a single-row approve that must not touch its
neighbours, a facet tab that must swap rather than navigate, and a row selection whose survival
across that swap is implemented entirely in JavaScript and is therefore invisible to every
server-side assertion in the repo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from playwright.async_api import expect
import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.helpers import open_shell, settled


pytestmark = pytest.mark.browser


if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from playwright.async_api import Page

    from tests.browser.seed import Seeder


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_bulk_preview_requires_confirmation_of_only_reviewed_eligible_rows(
    page_at: Callable[..., AbstractAsyncContextManager[Page]], seed: Seeder, theme: str, tmp_path: Path
) -> None:
    eligible = await seed.proposal(filename="preview-high.mp3", confidence=0.99)
    low = await seed.proposal(filename="preview-low.mp3", confidence=0.95)
    async with page_at(viewport="phone", theme=theme) as page:
        await open_shell(page, "/s/rename?threshold=99")
        patches: list[str] = []
        page.on("request", lambda request: patches.append(request.url) if request.method == "PATCH" else None)
        for name in ("preview-high.mp3", "preview-low.mp3"):
            await page.get_by_role("checkbox", name=f"Select {name}", exact=True).check()
        await page.get_by_role("button", name="Preview eligible selection", exact=True).click()
        preview = page.get_by_role("region", name="Bulk approval preview")
        await preview.get_by_text("1 eligible at 99% · 1 excluded from selection", exact=True).wait_for()
        assert patches == []
        assert await preview.locator('input[name="review_tokens"]').count() == 1
        assert str(eligible.id) in (await preview.locator('input[name="review_tokens"]').input_value())
        assert await page.locator(f"#rename-row-{eligible.id}").get_by_role("button", name="APPROVE", exact=True).count() == 1
        await preview.scroll_into_view_if_needed()
        await page.screenshot(path=str(tmp_path / f"bulk-preview-{theme}.png"), full_page=True)
        page.once("dialog", lambda dialog: dialog.dismiss())
        await preview.get_by_role("button", name="Approve these 1 previewed proposals", exact=True).click()
        assert patches == []
        page.once("dialog", lambda dialog: dialog.accept())
        await preview.get_by_role("button", name="Approve these 1 previewed proposals", exact=True).click()
        await page.get_by_text("1 approved, 0 skipped.", exact=True).wait_for()
        assert len(patches) == 1
        assert await page.locator(f"#rename-row-{eligible.id}").count() == 0
        assert await page.locator(f"#rename-row-{low.id}").count() == 1
        undo = page.get_by_role("button", name="Undo this approval batch", exact=True)
        await undo.scroll_into_view_if_needed()
        await page.screenshot(path=str(tmp_path / f"bulk-undo-{theme}.png"), full_page=True)
        page.once("dialog", lambda dialog: dialog.accept())
        await undo.click()
        await page.get_by_text("1 reverted, 0 skipped. Edited, executed, or conflicting versions cannot be reverted.", exact=True).wait_for()
        assert len(patches) == 2
        assert await page.locator(f"#rename-row-{eligible.id}").count() == 1
        assert await page.locator(f"#rename-row-{low.id}").count() == 1
        assert await page.locator("#changes-confidence-threshold").input_value() == "99"


async def test_approving_one_proposal_swaps_only_that_row(page: Any, seed: Any) -> None:
    """APPROVE re-renders its own row and leaves every sibling exactly as it was.

    The row's markup says ``hx-target="#rename-row-{id}" hx-swap="outerHTML"``, and the server suite
    proves the response is the one targeted row. Neither establishes what this asserts: that htmx
    put the response THERE. A target typo, a duplicated id, or an ``hx-swap`` widened during a
    refactor all produce a response that is correct on the wire and lands somewhere else -- most
    destructively on the whole list, which silently discards every other row's edit-in-progress and
    checkbox state.

    Approving is also the only state transition on this page that is irreversible from the
    operator's side within one gesture, so "which rows did that touch" is the question worth
    holding.
    """
    target = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    bystander = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-02>.mp3")

    await open_shell(page, "/s/rename")
    bystander_before = await page.locator(f"#rename-row-{bystander.id}").inner_html()

    await page.click(f"#rename-row-{target.id} button:has-text('APPROVE')")
    await settled(page)

    approved = page.locator(f"#rename-row-{target.id}")
    await approved.get_by_text("APPROVED").wait_for()
    assert await approved.locator("button:has-text('APPROVE')").count() == 0, "the approved row still offers APPROVE — the swap did not land on it"
    assert await approved.locator("button:has-text('UNDO')").count() == 1, "an approved row must offer UNDO"

    assert await page.locator(f"#rename-row-{bystander.id}").inner_html() == bystander_before, (
        "approving one proposal re-rendered a different row — the swap was wider than its hx-target"
    )
    assert await page.locator("[id^='rename-row-']").count() == 2, "the row count changed; the response replaced the list rather than one row"


async def test_a_facet_tab_swaps_the_list_and_pushes_the_url_without_reloading(page: Any, seed: Any) -> None:
    """The Changes Review tabs are an htmx swap with ``hx-push-url``, not a navigation.

    This is the "search/tab state" flow the bead names. The tabs carry both ``href`` and ``hx-get``
    -- so they degrade to real links without JavaScript, which is why a broken ``hx-get`` does not
    look broken: the page still changes, it just does it by reloading the entire document. That
    difference is invisible in a screenshot and invisible on the server, and it costs every piece of
    unsaved client state on the page. The sentinel below is the whole test: it survives a swap and
    does not survive a load.
    """
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.APPROVED, filename="<set-02>.mp3")

    await open_shell(page, "/s/rename")
    await page.evaluate("window.__documentAlive = true")

    tabs = page.locator("nav[aria-label='Changes Review filters'] a")
    await tabs.filter(has_text="Approved").click()
    await settled(page)

    assert await page.evaluate("window.__documentAlive === true"), "the tab click reloaded the document — hx-get did not intercept the href"
    assert "status=approved" in page.url, "hx-push-url did not put the active facet in the address bar"

    current = page.locator("nav[aria-label='Changes Review filters'] a[aria-current='page']")
    assert await current.count() == 1, "exactly one tab must be marked aria-current after the swap"
    assert "Approved" in await current.inner_text()

    # The facet actually filtered, rather than merely relabelling itself.
    rows = page.locator("[id^='rename-row-']")
    assert await rows.count() == 1, f"the Approved facet rendered {await rows.count()} rows, expected the 1 approved proposal"
    assert await rows.first.locator("button:has-text('UNDO')").count() == 1, "the row shown under Approved is not an approved row"


async def test_a_row_selection_is_written_into_the_url_and_into_every_nav_attribute(page: Any, seed: Any) -> None:
    """Checking a row writes ``selected`` into the URL and into every nav target's ATTRIBUTE.

    ``_changes_list.html``'s ``sync()`` does three things on every selection change: rewrites the
    address bar via ``history.replaceState``, rewrites the ``href``/``hx-get`` of each
    ``[data-changes-nav]`` element, and rewrites the bulk form's ``hx-patch``. All three are pure
    client-side DOM mutation with no server involvement whatsoever, so this behaviour has no
    server-side test and can have none.

    Two corrections from phaze-tp52f, both of which this docstring got wrong and which are worth
    keeping written down rather than quietly edited away:

    1. **This test does not change a tab, and never did** -- it was named as though it did. Every
       assertion below reads an ATTRIBUTE, and the attribute was correct for the entire lifetime of
       the defect phaze-tp52f fixed: htmx 2.0.10 captured a verb's path in the trigger-handler
       closure at ``processNode`` time, so the rewritten ``hx-get`` never reached the wire and a tab
       change dropped (or resurrected) the selection with all of these assertions still green. What
       actually crosses a tab boundary, and reads the socket rather than the DOM, is
       ``tests/browser/test_changes_facet_selection_wire.py``. This file keeps the attribute half,
       which is a real and cheap contract; it is simply not the half that was ever in doubt.
    2. **The bulk approve write is NOT keyed on this query.** ``PATCH /proposals/bulk`` scopes from
       ``review_tokens``/``proposal_ids`` in the request BODY, which htmx serializes from the live
       DOM at request time; ``selected`` is read only by ``build_changes_review_context``, to seed
       the RESPONSE's checkbox state. So a dropped ``selected`` never approved "a different set than
       the one on screen" directly. It does so indirectly -- the checkbox it resurrects IS the bulk
       scope -- which is a slower and more visible failure than the one claimed here.
    """
    keeper = await seed.proposal(status=ProposalStatus.PENDING, filename="<set-01>.mp3")
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-02>.mp3")

    await open_shell(page, "/s/rename")
    assert "selected=" not in page.url, "nothing is selected yet, so the URL must not claim otherwise"

    # The selection checkbox is a SIBLING of the diff row, not a child of it -- _changes_list.html
    # wraps each row in a flex pair so the control sits outside the swap target the row's own
    # approve/edit actions replace. Addressed by its accessible name, which is also the only thing
    # that identifies it to a screen reader.
    checkbox = page.get_by_label("Select <set-01>.mp3")
    await checkbox.check()
    await page.wait_for_function("() => window.location.search.includes('selected=')")
    assert str(keeper.id) in page.url, "the checked row's id did not reach the address bar"

    # Every nav destination must carry the selection forward, not just the address bar.
    nav_targets = await page.locator("[data-changes-nav]").evaluate_all("els => els.map(el => el.getAttribute('hx-get'))")
    assert nav_targets, "the changes list rendered no [data-changes-nav] elements to carry the selection"
    assert all(f"selected={keeper.id}" in (target or "") for target in nav_targets), (
        f"a nav target dropped the selection: {[t for t in nav_targets if f'selected={keeper.id}' not in (t or '')]}"
    )

    # And the bulk form -- the actual write -- agrees with what is on screen.
    bulk_action = await page.locator("#changes-bulk-form").get_attribute("hx-patch")
    assert f"selected={keeper.id}" in (bulk_action or ""), f"the bulk approve form would submit {bulk_action!r}, which does not carry the checked row"

    # Unchecking must retract it everywhere, or a stale id survives into a later bulk write.
    await checkbox.uncheck()
    await page.wait_for_function("() => !window.location.search.includes('selected=')")
    assert "selected=" not in (await page.locator("#changes-bulk-form").get_attribute("hx-patch") or ""), (
        "unchecking the row left the selection in the bulk form's hx-patch"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_slider_preview_pairs_changes_posts_exact_tokens_and_counts_stale_skip(
    page_at: Callable[..., AbstractAsyncContextManager[Page]], seed: Seeder, theme: str, tmp_path: Path
) -> None:
    from datetime import timedelta
    from urllib.parse import parse_qs

    high = await seed.proposal(
        filename="slider-high.mp3", proposed_filename="Renamed High.mp3", proposed_path="/music/Reviewed/Event", confidence=0.99
    )
    low = await seed.proposal(filename="slider-low.mp3", confidence=0.95)
    async with page_at(viewport="phone", theme=theme) as page:
        await page.emulate_media(reduced_motion="reduce")
        await open_shell(page, "/s/rename?status=all&q=retained&sort=filename&order=desc&page_size=50")
        preview = page.get_by_role("region", name="Bulk approval preview")
        await preview.get_by_text("2 eligible at 90%", exact=True).wait_for()
        slider = page.get_by_label("Minimum confidence:", exact=False)
        await slider.focus()
        await slider.press("End")
        await preview.get_by_text("1 eligible at 99%", exact=True).wait_for()
        assert await page.locator('output[for="changes-confidence-threshold"]').inner_text() == "99%"
        assert await slider.evaluate("el => getComputedStyle(el).transitionDuration") == "0s"
        row = preview.locator(f'[data-preview-proposal="{high.id}"]')
        assert "slider-high.mp3" in await row.inner_text() and "Renamed High.mp3" in await row.inner_text()
        assert "/music/Reviewed/Event/Renamed High.mp3" in await row.inner_text()
        tokens = await preview.locator('input[name="review_tokens"]').evaluate_all("elements => elements.map(el => el.value)")
        assert len(tokens) == 1 and tokens[0].startswith(str(high.id) + "|")
        high.proposed_path = "/music/Changed/Unreviewed"
        high.updated_at += timedelta(seconds=10)
        await seed.session.commit()
        requests: list[Any] = []
        page.on("request", lambda request: requests.append(request) if request.method == "PATCH" and "/proposals/bulk?" in request.url else None)
        page.once("dialog", lambda dialog: dialog.dismiss())
        await preview.get_by_role("button", name="Approve these 1 previewed proposals", exact=True).click()
        assert requests == []
        page.once("dialog", lambda dialog: dialog.accept())
        await preview.get_by_role("button", name="Approve these 1 previewed proposals", exact=True).click()
        await page.get_by_text("0 approved, 1 skipped.", exact=False).wait_for()
        assert len(requests) == 1
        body = parse_qs(requests[0].post_data)
        assert body["review_tokens"] == tokens and body["threshold"] == ["99"]
        query = parse_qs(requests[0].url.split("?", 1)[1], keep_blank_values=True)
        assert (
            query["status"] == ["all"]
            and query["q"] == ["retained"]
            and query["sort"] == ["filename"]
            and query["order"] == ["desc"]
            and query["page_size"] == ["50"]
            and query["threshold"] == ["99"]
        )
        assert await page.get_by_role("button", name="Undo this approval batch", exact=True).count() == 0
        await seed.session.refresh(high)
        await seed.session.refresh(low)
        assert high.status == low.status == ProposalStatus.PENDING.value
        await page.screenshot(path=str(tmp_path / f"a5-slider-stale-{theme}.png"), full_page=True)


@pytest.mark.parametrize("release_before_current", [True, False])
async def test_selected_preview_response_cannot_restore_old_selection(page: Page, seed: Seeder, release_before_current: bool) -> None:
    import asyncio
    from urllib.parse import parse_qs

    from playwright.async_api import Error as PlaywrightError

    first = await seed.proposal(filename="race-first.mp3", confidence=0.99)
    second = await seed.proposal(filename="race-second.mp3", confidence=0.99)
    await open_shell(page, "/s/rename")
    preview = page.get_by_role("region", name="Bulk approval preview")
    await preview.get_by_text("2 eligible at 90%", exact=True).wait_for()
    held = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    generations: list[str] = []
    hold_next = True

    async def delay_selected(route: Any) -> None:
        nonlocal hold_next
        body = parse_qs(route.request.post_data)
        if body.get("scope", ["selected"])[0] == "selected" and hold_next:
            hold_next = False
            generations.extend(body["preview_generation"])
            response = await route.fetch()
            held.set()
            await release.wait()
            try:
                await route.fulfill(response=response)
            except PlaywrightError:
                # Native hx-sync replacement can abort this already-read response.
                pass
            finally:
                completed.set()
        else:
            await route.continue_()

    await page.route("**/proposals/bulk/preview?**", delay_selected)
    patches: list[Any] = []
    page.on("request", lambda request: patches.append(request) if request.method == "PATCH" else None)
    await page.get_by_label("Select race-first.mp3", exact=True).check()
    await page.get_by_role("button", name="Preview eligible selection", exact=True).click()
    await asyncio.wait_for(held.wait(), timeout=10)
    await page.get_by_label("Select race-first.mp3", exact=True).uncheck()
    await page.get_by_label("Select race-second.mp3", exact=True).check()
    assert await page.locator('#changes-approval-preview input[name="review_tokens"]').count() == 0
    assert await page.locator("#changes-approval-preview button").count() == 0
    assert len(generations) == 1 and int(generations[0]) > 0
    try:
        if release_before_current:
            async with page.expect_response(lambda response: "/proposals/bulk/preview?" in response.url):
                release.set()
            await asyncio.wait_for(completed.wait(), timeout=10)
            await page.wait_for_function("() => document.querySelector('[data-selected-preview]').disabled === false")
            assert await page.locator('#changes-approval-preview input[name="review_tokens"]').count() == 0
            await page.get_by_role("button", name="Preview eligible selection", exact=True).click()
            await preview.get_by_text("1 eligible at 90% · 0 excluded from selection", exact=True).wait_for()
            expected = {str(second.id)}
        else:
            await page.get_by_role("button", name="Refresh preview", exact=True).click()
            await preview.get_by_text("2 eligible at 90%", exact=True).wait_for()
            release.set()
            await asyncio.wait_for(completed.wait(), timeout=10)
            expected = {str(first.id), str(second.id)}
        tokens = await preview.locator('input[name="review_tokens"]').evaluate_all("els => els.map(el => el.value.split('|')[0])")
        assert set(tokens) == expected
        # The swapped-in button is released by the htmx guard a beat after its text appears, so poll for it
        # rather than sampling the disabled flag once.
        await expect(preview.get_by_role("button", name=f"Approve these {len(expected)} previewed proposals", exact=True)).to_be_enabled()
        assert not patches
        await seed.session.refresh(first)
        await seed.session.refresh(second)
        assert first.status == second.status == ProposalStatus.PENDING.value
    finally:
        release.set()


async def test_failed_preview_clears_confirmation_and_refresh_recovers(page: Page, seed: Seeder) -> None:
    proposal = await seed.proposal(filename="failed-preview.mp3", confidence=0.99)
    await open_shell(page, "/s/rename")
    preview = page.get_by_role("region", name="Bulk approval preview")
    await preview.get_by_text("1 eligible at 90%", exact=True).wait_for()
    patches: list[Any] = []
    page.on("request", lambda request: patches.append(request) if request.method == "PATCH" else None)
    await page.route("**/proposals/bulk/preview?**", lambda route: route.fulfill(status=503, body="Synthetic preview failure"))
    await page.get_by_role("button", name="Refresh preview", exact=True).click()
    await page.get_by_role("alert").filter(has_text="Preview unavailable").wait_for()
    assert await page.locator('#changes-approval-preview input[name="review_tokens"]').count() == 0
    assert await page.locator("#changes-approval-preview button").count() == 0
    assert await page.locator('#changes-bulk-form button[type="submit"][value="approve_eligible"]').count() == 0
    await page.unroute("**/proposals/bulk/preview?**")
    await page.get_by_role("button", name="Refresh preview", exact=True).click()
    await preview.get_by_text("1 eligible at 90%", exact=True).wait_for()
    assert (await preview.locator('input[name="review_tokens"]').input_value()).startswith(str(proposal.id) + "|")
    assert not patches


async def test_batch_undo_retains_new_active_threshold(page: Page, seed: Seeder) -> None:
    from urllib.parse import parse_qs

    proposal = await seed.proposal(filename="undo-threshold.mp3", confidence=0.99)
    await open_shell(page, "/s/rename?status=all&q=retained&sort=filename&order=desc")
    preview = page.get_by_role("region", name="Bulk approval preview")
    await preview.get_by_text("1 eligible at 90%", exact=True).wait_for()
    page.once("dialog", lambda dialog: dialog.accept())
    await preview.get_by_role("button", name="Approve these 1 previewed proposals", exact=True).click()
    await page.get_by_text("1 approved, 0 skipped.", exact=True).wait_for()
    await page.locator("[data-action-status]").wait_for(state="detached", timeout=8000)
    assert await page.get_by_role("button", name="Undo this approval batch", exact=True).is_enabled()
    slider = page.get_by_label("Minimum confidence:", exact=False)
    await slider.focus()
    await slider.press("End")
    await page.locator('output[for="changes-confidence-threshold"]').get_by_text("99%", exact=True).wait_for()
    page.once("dialog", lambda dialog: dialog.accept())
    async with page.expect_request(lambda request: request.method == "PATCH" and "/proposals/bulk/undo?" in request.url) as request_info:
        await page.get_by_role("button", name="Undo this approval batch", exact=True).click()
    request = await request_info.value
    assert parse_qs(request.post_data)["threshold"] == ["99"]
    query = parse_qs(request.url.split("?", 1)[1])
    assert query["threshold"] == ["99"] and query["q"] == ["retained"] and query["sort"] == ["filename"]
    await page.get_by_text("1 reverted, 0 skipped. Edited, executed, or conflicting versions cannot be reverted.", exact=True).wait_for()
    assert await page.locator("#changes-confidence-threshold").input_value() == "99"
    await seed.session.refresh(proposal)
    assert proposal.status == ProposalStatus.PENDING.value


async def test_population_cap_displays_and_confirms_exactly_500_operations(
    page_at: Callable[..., AbstractAsyncContextManager[Page]], seed: Seeder, tmp_path: Path
) -> None:
    from urllib.parse import parse_qs
    import uuid

    from sqlalchemy import func, select

    from phaze.models.file import FileRecord
    from phaze.models.proposal import RenameProposal

    agent = await seed.agent()
    for index in range(501):
        file_id = uuid.uuid4()
        filename = f"cap-source-{index}.mp3"
        seed.session.add(
            FileRecord(
                id=file_id,
                agent_id=agent.id,
                sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                original_path=f"/archive-mount/incoming/{filename}",
                original_filename=filename,
                current_path=f"/archive-mount/incoming/{filename}",
                file_type="mp3",
                file_size=100,
            )
        )
        seed.session.add(
            RenameProposal(
                id=uuid.UUID(int=index + 1),
                file_id=file_id,
                proposed_filename=f"cap-target-{index}.mp3",
                proposed_path="/archive-mount/Reviewed/Event",
                confidence=0.99,
                status=ProposalStatus.PENDING.value,
            )
        )
    await seed.session.commit()
    async with page_at(viewport="phone", theme="dark") as page:
        await open_shell(page, "/s/rename?threshold=99")
        preview = page.get_by_role("region", name="Bulk approval preview")
        await preview.get_by_text("500 eligible at 99%", exact=True).wait_for()
        assert "More than 500 eligible proposals match" in await preview.inner_text()
        rows = preview.locator("[data-preview-proposal]")
        assert await rows.count() == 500
        tokens = await preview.locator('input[name="review_tokens"]').evaluate_all("els => els.map(el => el.value)")
        visible_ids = await rows.evaluate_all("els => els.map(el => el.dataset.previewProposal)")
        assert [token.split("|")[0] for token in tokens] == visible_ids
        assert str(uuid.UUID(int=501)) not in visible_ids
        assert "cap-source-0.mp3" in await rows.first.inner_text()
        assert "/archive-mount/Reviewed/Event/cap-target-0.mp3" in await rows.first.inner_text()
        await preview.scroll_into_view_if_needed()
        await page.screenshot(path=str(tmp_path / "a5-cap-dark-phone.png"), full_page=True)
        page.once("dialog", lambda dialog: dialog.accept())
        async with page.expect_request(lambda request: request.method == "PATCH" and "/proposals/bulk?" in request.url) as request_info:
            await preview.get_by_role("button", name="Approve these 500 previewed proposals", exact=True).click()
        request = await request_info.value
        assert parse_qs(request.post_data)["review_tokens"] == tokens
        await page.get_by_text("500 approved, 0 skipped.", exact=True).wait_for()
        approved = await seed.session.scalar(
            select(func.count()).select_from(RenameProposal).where(RenameProposal.status == ProposalStatus.APPROVED.value)
        )
        pending = await seed.session.scalar(
            select(func.count()).select_from(RenameProposal).where(RenameProposal.status == ProposalStatus.PENDING.value)
        )
        assert approved == 500 and pending == 1


async def test_old_threshold_response_cannot_replace_current_99_preview(page: Page, seed: Seeder) -> None:
    import asyncio
    from urllib.parse import parse_qs

    from playwright.async_api import Error as PlaywrightError

    high = await seed.proposal(filename="threshold-high.mp3", confidence=0.99)
    low = await seed.proposal(filename="threshold-low.mp3", confidence=0.95)
    await open_shell(page, "/s/rename")
    preview = page.get_by_role("region", name="Bulk approval preview")
    await preview.get_by_text("2 eligible at 90%", exact=True).wait_for()
    held = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    requests: list[dict[str, list[str]]] = []

    async def delay_old_threshold(route: Any) -> None:
        body = parse_qs(route.request.post_data)
        requests.append(body)
        if body["threshold"] == ["90"]:
            response = await route.fetch()
            held.set()
            await release.wait()
            try:
                await route.fulfill(response=response)
            except PlaywrightError:
                pass  # Native hx-sync cancels the superseded request.
            finally:
                completed.set()
        else:
            await route.continue_()

    await page.route("**/proposals/bulk/preview?**", delay_old_threshold)
    await page.get_by_role("button", name="Refresh preview", exact=True).click()
    await asyncio.wait_for(held.wait(), timeout=10)
    slider = page.get_by_label("Minimum confidence:", exact=False)
    try:
        await slider.focus()
        await slider.press("ArrowRight")
        await slider.press("End")
        await preview.get_by_text("1 eligible at 99%", exact=True).wait_for()
        tokens = await preview.locator('input[name="review_tokens"]').evaluate_all("els => els.map(el => el.value)")
        assert len(tokens) == 1 and tokens[0].startswith(str(high.id) + "|")
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=10)
        assert await preview.locator('input[name="review_tokens"]').evaluate_all("els => els.map(el => el.value)") == tokens
        assert await preview.locator(f'[data-preview-proposal="{low.id}"]').count() == 0
        assert await preview.locator('input[name="threshold"]').input_value() == "99"
        await expect(preview.get_by_role("button", name="Approve these 1 previewed proposals", exact=True)).to_be_enabled()
        assert int(requests[-1]["preview_generation"][0]) > int(requests[0]["preview_generation"][0])
    finally:
        release.set()


async def test_live_row_eligibility_and_description_follow_threshold_and_row_swaps(page: Page, seed: Seeder) -> None:
    target = await seed.proposal(filename="live-floor.mp3", confidence=0.95)
    unknown = await seed.proposal(filename="live-unknown.mp3", confidence=None)
    resolved = await seed.proposal(filename="live-resolved.mp3", confidence=0.99, status=ProposalStatus.APPROVED)
    collision = await seed.proposal(
        filename="live-collision.mp3", confidence=0.99, proposed_filename="same.mp3", proposed_path="/synthetic/collision"
    )
    await seed.proposal(filename="live-collision-other.mp3", confidence=0.99, proposed_filename="same.mp3", proposed_path="/synthetic/collision")
    await open_shell(page, "/s/rename?status=all")
    slider = page.get_by_label("Minimum confidence:", exact=False)
    cue = page.locator(f"#changes-eligibility-{target.id}")
    checkbox = page.get_by_role("checkbox", name="Select live-floor.mp3", exact=True)
    assert await checkbox.get_attribute("aria-describedby") == f"changes-eligibility-{target.id}"
    assert "Eligible" in await cue.inner_text()
    row = page.locator(f"#rename-row-{target.id}")
    await slider.focus()
    await slider.press("End")
    await settled(page)
    assert "Excluded" in await cue.inner_text() and "99%" in await cue.inner_text()
    assert "Below the 99%" in await row.locator("[data-live-confidence-warning]").inner_text()
    await slider.press("Home")
    await settled(page)
    assert "Eligible" in await cue.inner_text() and "90%" in await cue.inner_text()
    assert not await row.locator("[data-live-confidence-warning]").is_visible()
    assert "unavailable" in await page.locator(f"#changes-eligibility-{unknown.id}").inner_text()
    assert "resolved" in await page.locator(f"#changes-eligibility-{resolved.id}").inner_text()
    assert "conflict" in await page.locator(f"#changes-eligibility-{collision.id}").inner_text()
    await row.get_by_role("button", name="APPROVE", exact=True).click()
    await row.get_by_role("button", name="UNDO", exact=True).wait_for()
    assert "resolved" in await cue.inner_text()
    await row.get_by_role("button", name="UNDO", exact=True).click()
    await row.get_by_role("button", name="APPROVE", exact=True).wait_for()
    assert "preview must confirm" in await cue.inner_text()
    assert await page.locator(f"#changes-eligibility-{target.id}").count() == 1
    await row.get_by_role("button", name="EDIT", exact=True).click()
    await row.locator('input[name="proposed"]').fill("live-floor-reviewed.mp3")
    await row.get_by_role("button", name="SAVE EDIT", exact=True).click()
    await row.get_by_role("button", name="APPROVE", exact=True).wait_for()
    assert "preview must confirm" in await cue.inner_text()
    await slider.focus()
    await slider.press("End")
    await settled(page)
    assert "Excluded" in await cue.inner_text() and "99%" in await cue.inner_text()
    await slider.press("Home")
    await settled(page)
    assert "90%" in await cue.inner_text() and "preview must confirm" in await cue.inner_text()
    assert not await row.locator("[data-live-confidence-warning]").is_visible()
