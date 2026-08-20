"""phaze-5i74w: neither approval-commit <form> may silently no-op on a premature click.

``duplicates/partials/review_response.html``'s "Confirm resolution" step and
``pipeline/partials/_changes_list.html``'s bulk tag-write approval both declared no ``method``/
``action`` and relied entirely on htmx intercepting their submit. htmx wires that interception up
(and, for the tag-write form, its ``hx-confirm`` prompt) during its deferred SETTLE phase, not
synchronously on swap -- see shell.html's ``htmx:load`` listener comment for the mechanism. A click
that landed in the gap between "attached to the DOM" and "processed by htmx" fell through to the
browser's own native submission: no method -> GET, no action -> the current URL, hidden fields ->
query parameters. Both forms ARE the approval step in this app's human-in-the-loop workflow, so that
fallback was a silent no-op indistinguishable from an ordinary reload -- measured via Playwright
network trace at 4/40 (10%) for a click fired the instant the confirm form attached to the DOM
(phaze-boyl9, 2026-08-20).

The fix renders each button ``disabled`` + ``data-hx-guard`` and releases the guard only once htmx's
own ``htmx:load`` event proves the element has been processed (shell.html). Playwright's own click
actionability rules refuse to click a disabled control, so a plain, unsynchronized ``page.click()``
issued the instant a guarded button exists now WAITS for it structurally -- there is no explicit
``settled()``/``click_swap`` call in either test below, which is deliberate: that is what "still
fails to race, without the test doing anything special" is supposed to look like post-fix. Every
test here reproduces the exact "click the instant it exists" methodology phaze-boyl9's PRE-fix test
used, not the ``click_swap``-guarded version it shipped with (AC4: that fix stays exactly as merged
in 303f7761 -- a test should not depend on a race even once the product tolerates it -- this file
is additional coverage, not a replacement for it).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.helpers import click_swap, open_shell


pytestmark = pytest.mark.browser


# Matches phaze-boyl9's own "cold shape" repeat count (a fresh navigation per attempt) -- the shape
# closest to a real operator, who never gets a warm in-process repeat either.
_DEDUPE_RACE_ATTEMPTS = 15

# The tag-write race needs a heavier per-attempt seed (a file + metadata + an executed proposal, vs
# dedupe's file-only duplicate group), so it runs fewer repeats -- still enough to make a ~10%
# pre-fix failure rate implausible to pass by chance.
_TAG_WRITE_RACE_ATTEMPTS = 6


async def test_confirming_a_duplicate_resolution_the_instant_the_button_exists_still_commits(page: Any, seed: Any) -> None:
    """Race the dedupe "Confirm resolution" click, repeatedly, with no settle wait of any kind.

    Step 1 (staging the plan) goes through ``click_swap`` exactly as phaze-boyl9's test does --
    it is not the button under test here, and racing it would just add noise. Step 2 (the confirm
    click that commits the resolution) is a bare ``page.click()`` issued the moment the confirm form
    exists, reproducing phaze-boyl9's PRE-fix repro precisely. Before phaze-5i74w's fix this was
    measured at 4/40 (10%) stray native-GET fallbacks; every attempt below must commit.
    """
    for attempt in range(_DEDUPE_RACE_ATTEMPTS):
        files = await seed.duplicate_group(count=3)
        group_hash = files[0].sha256_hash

        await open_shell(page, "/s/dedupe")
        await page.evaluate("window.__documentAlive = true")

        await click_swap(page, f"#dupe-group-{group_hash} button[type=submit]")

        # THE RACE: no wait between the swap that just inserted this form and this click.
        await page.click(f"#dupe-group-{group_hash} button[type=submit]")

        assert await page.evaluate("window.__documentAlive === true"), (
            f"attempt {attempt}: the confirm click fell through to a native form submission "
            "(a full page navigation), which is exactly the pre-fix bug"
        )
        toasts = page.locator("#toast-container > *")
        await toasts.first.wait_for(timeout=5_000)
        assert await toasts.count() == 1, f"attempt {attempt}: no resolve toast landed — the confirm click did not commit"
        assert "plan_id=" not in page.url, (
            f"attempt {attempt}: the confirm click fell through to the native GET fallback (plan_id promoted to a query param)"
        )


async def test_approving_bulk_tag_writes_the_instant_the_form_swaps_in_still_confirms_and_writes(page: Any, seed: Any) -> None:
    """AC3: the raced click must still show ``hx-confirm``'s prompt, not just still write the tags.

    Unlike the dedupe form, the bulk tag-write form is not itself reached via a two-step reveal --
    it is present as soon as the Changes Review workspace swaps in, so the race here is between that
    workspace swap (a real rail-nav ``hx-get`` into ``#stage-workspace``, not a full page load —
    htmx's initial-boot ``processNode(document.body)`` is synchronous and does not carry this race;
    only an AJAX swap's deferred settle task does) and the very first click on its button.

    A fix that only stopped the native-GET fallback without restoring the confirmation would be a
    different, smaller bug than the one filed: the destructive bulk write would proceed with no
    prompt at all. This asserts BOTH halves: a ``dialog`` event fires with the expected message
    (proving ``hx-confirm`` is live, not skipped), and accepting it actually queues the write (the
    same toast-in-``#toast-container`` signal the dedupe form uses for "the POST truly happened").
    """
    for attempt in range(_TAG_WRITE_RACE_ATTEMPTS):
        await seed.bulk_eligible_tag_change(filename=f"Artist - Race Track {attempt}.mp3")

        # Start somewhere other than the Changes Review workspace so the form under test is reached
        # by a real htmx swap, not folded into the initial full-page HTML.
        await open_shell(page, "/s/summary")
        await page.evaluate("window.__documentAlive = true")

        dialog_messages: list[str] = []

        async def _accept(dialog: Any, messages: list[str] = dialog_messages) -> None:
            messages.append(dialog.message)
            await dialog.accept()

        page.once("dialog", _accept)

        await page.click("[data-rail-stage='rename']")
        # THE RACE: no settled()/click_swap between that nav swap and this click.
        await page.click("button:has-text('Approve visible eligible tag writes')")

        assert await page.evaluate("window.__documentAlive === true"), (
            f"attempt {attempt}: the approval click fell through to a native form submission"
        )
        toasts = page.locator("#toast-container > *")
        await toasts.first.wait_for(timeout=5_000)
        assert dialog_messages, f"attempt {attempt}: no confirm dialog fired — a raced click bypassed hx-confirm on a destructive bulk write"
        assert "Approve the eligible tag changes" in dialog_messages[-1], f"attempt {attempt}: unexpected confirm prompt: {dialog_messages[-1]!r}"
        toast_text = await toasts.first.inner_text()
        assert "tag write" in toast_text and "queued" in toast_text, f"attempt {attempt}: the approval did not actually queue a write: {toast_text!r}"
