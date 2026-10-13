"""Native keys activate existing guarded row controls and advance only after a successful swap."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.browser.helpers import open_shell, swap_settles


pytestmark = pytest.mark.browser


async def focused(page: Any, row_id: str) -> None:
    await page.wait_for_function("id => document.activeElement.id === id && document.getElementById(id).dataset.reviewFocused === 'true'", arg=row_id)


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_native_triage_keys_use_row_routes_tokens_and_advance(page_at: Any, seed: Any, viewport: str, theme: str) -> None:
    for index in range(3):
        await seed.proposal(filename=f"song-{index}.mp3")
    async with page_at(viewport=viewport, theme=theme) as page:
        await open_shell(page, "/s/rename?status=all")
        rows = await page.locator("[data-review-row]").evaluate_all("els => els.map(el => el.id)")
        first, second, third = rows
        writes: list[Any] = []
        page.on("request", lambda request: writes.append(request) if request.method == "PATCH" else None)
        assert await page.locator("[data-review-hotkeys]").is_visible()
        assert "J/K move" in await page.locator("[data-review-hotkeys]").inner_text()
        await page.keyboard.press("j")
        await focused(page, second)
        await page.keyboard.press("k")
        await focused(page, first)
        assert not writes
        for key, row_id, action, next_row in [("a", first, "approve", second), ("s", second, "skip", third)]:
            button = page.locator(f"#{row_id} [data-review-action={action}]")
            values = json.loads(await button.get_attribute("hx-vals"))
            url = await button.get_attribute("hx-patch")
            if key == "s":
                page.once("dialog", lambda dialog: dialog.accept())
            async with swap_settles(page):
                await page.keyboard.press(key)
            await focused(page, next_row)
            assert writes[-1].url.endswith(url)
            assert parse_qs(writes[-1].post_data)["expected_updated_at"] == [values["expected_updated_at"]]
        await page.keyboard.press("k")
        await page.keyboard.press("k")
        await focused(page, first)
        undo = page.locator(f"#{first} [data-review-action=undo]")
        values = json.loads(await undo.get_attribute("hx-vals"))
        url = await undo.get_attribute("hx-patch")
        async with swap_settles(page):
            await page.keyboard.press("z")
        await focused(page, second)
        assert writes[-1].url.endswith(url)
        assert parse_qs(writes[-1].post_data)["expected_updated_at"] == [values["expected_updated_at"]]
        assert len(writes) == 3
        assert await page.locator(f"#{first} [data-review-action=approve]").is_visible()
        assert await page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1


async def test_typing_dialogs_modifiers_and_cancelled_skip_are_inert(page: Any, seed: Any) -> None:
    await seed.proposal(filename="typing.mp3")
    await seed.proposal(filename="sibling.mp3")
    await open_shell(page, "/s/rename?status=all")
    first = await page.locator("[data-review-row]").first.get_attribute("id")
    writes: list[Any] = []
    page.on("request", lambda request: writes.append(request) if request.method == "PATCH" else None)
    await page.locator(f"#{first}").focus()
    await page.keyboard.press("e")
    field = page.locator(f"#{first} input[name=proposed]")
    await page.wait_for_function("selector => document.querySelector(selector) === document.activeElement", arg=f"#{first} input[name=proposed]")
    await field.fill("")
    await page.keyboard.type("aes zjk")
    assert await field.input_value() == "aes zjk"
    assert not writes
    await page.locator(f"#{first}").get_by_role("button", name="DISCARD").click()
    for tag in ["textarea", "select", "contenteditable"]:
        await page.evaluate(
            """tag => {
                const el = document.createElement(tag === 'contenteditable' ? 'div' : tag);
                if (tag === 'contenteditable') el.contentEditable = 'true';
                el.id = 'typing-guard'; document.querySelector('[data-review-row]').appendChild(el); el.focus();
            }""",
            tag,
        )
        for key in "aeszjk":
            await page.keyboard.press(key)
        assert not writes
        await page.locator("#typing-guard").evaluate("el => el.remove()")
    await page.evaluate(
        "() => { const d = document.createElement('dialog'); d.id = 'keyboard-dialog'; document.body.appendChild(d); d.showModal(); }"
    )
    for key in "aeszjk":
        await page.keyboard.press(key)
    assert not writes
    await page.keyboard.press("Escape")
    await page.locator(f"#{first}").focus()
    await page.keyboard.press("Control+a")
    assert not writes
    page.once("dialog", lambda dialog: dialog.dismiss())
    await page.keyboard.press("s")
    assert not writes
    await focused(page, first)
    await page.keyboard.press("j")
    assert await page.evaluate("document.activeElement.id") != first, "cancelled confirmation must not leave the keyboard model busy"


async def test_tag_shortcut_preserves_the_tag_review_token_and_post_route(page: Any, seed: Any) -> None:
    await seed.bulk_eligible_tag_change(filename="Example Artist - First Track.mp3")
    await seed.bulk_eligible_tag_change(filename="Example Artist - Second Track.mp3")
    await open_shell(page, "/s/rename?status=all")
    rows = await page.locator('[id^="tagwrite-row-"]').evaluate_all("els => els.map(el => el.id)")
    first, second = rows
    button = page.locator(f"#{first} [data-review-action=approve]")
    values = json.loads(await button.get_attribute("hx-vals"))
    url = await button.get_attribute("hx-post")
    await page.locator(f"#{first}").focus()
    async with page.expect_request(lambda request: request.url.endswith(url) and request.method == "POST") as request, swap_settles(page):
        await page.keyboard.press("a")
    await focused(page, second)
    assert parse_qs((await request.value).post_data)["review_token"] == [values["review_token"]]
    assert "QUEUED" in (await page.locator(f"#{first}").inner_text()).upper()


@pytest.mark.parametrize("action,key,status", [("approve", "a", "pending"), ("reject", "s", "pending"), ("undo", "z", "approved")])
async def test_real_stale_versions_keep_row_conflict_and_focus(page: Any, seed: Any, action: str, key: str, status: str) -> None:
    from phaze.models.proposal import ProposalStatus

    proposal = await seed.proposal(filename="stale.mp3", status=ProposalStatus(status))
    await seed.proposal(filename="other.mp3")
    await open_shell(page, "/s/rename?status=all")
    row_id = f"rename-row-{proposal.id}"
    row = page.locator(f"#{row_id}")
    await row.focus()
    await row.evaluate("el => window.__reviewedRow = el")
    proposal.updated_at += timedelta(seconds=1)
    proposal.proposed_filename = "changed-after-review.mp3"
    await seed.session.commit()
    if key == "s":
        page.once("dialog", lambda dialog: dialog.accept())
    async with page.expect_response(lambda response: urlsplit(response.url).path == f"/proposals/{proposal.id}/{action}") as result:
        await page.keyboard.press(key)
    assert (await result.value).status == 409
    # ab20b472 added a generic persistent action-failure alert beside the row's own conflict alert, so
    # select the conflict by what it says rather than expecting the row to hold exactly one alert.
    conflict = row.get_by_role("alert").filter(has_text="changed")
    await conflict.wait_for(state="visible")
    assert "changed" in (await conflict.inner_text()).lower()
    await focused(page, row_id)
    assert await row.evaluate("el => el === window.__reviewedRow")
    assert await row.locator("code[data-diff-side=after]").first.inner_text() != "changed-after-review.mp3"
