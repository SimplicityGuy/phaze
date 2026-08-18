"""phaze-tzy6s.14 (CR-14-2): the Metadata workspace's actions.

``/s/metadata`` was reached once by the all-workspace console sweep and nothing on it was ever
clicked. Its actions are where the two contract properties AC 2 names -- confirmation boundaries and
disabled-state explanations -- are actually implemented, and both are runtime-only:

* ``hx-confirm`` is a string in the markup. Whether a *dismissed* confirm suppresses the request is
  a property of htmx's dialog handling, and a server-side test that reads the attribute proves the
  operator was asked, never that answering "no" stopped anything.
* ``:disabled="$store.pipeline.metadataBusy === 0"`` is an Alpine binding. Server-side it is literal
  text; the boolean it evaluates to is computed in the browser from a store the workspace seeds and
  the shell's poll updates.

The third test covers the deferred load, which is the failure mode of putting ``hx-trigger="load"``
inside a fragment that arrives by swap rather than by navigation.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.browser.helpers import click_swap, open_shell, settled, wait_for_stage


pytestmark = pytest.mark.browser


_EXTRACT = 'button[aria-label="Extract metadata for all pending files"]'


def _watch_extract_posts(page: Any) -> list[str]:
    """Record every POST the Extract All control could have made."""
    posted: list[str] = []
    page.on(
        "request",
        lambda request: posted.append(request.url) if request.method == "POST" and "/pipeline/extract-metadata" in request.url else None,
    )
    return posted


async def test_dismissing_the_extract_confirm_enqueues_nothing(page: Any, seed: Any) -> None:
    """The confirmation BOUNDARY: answering "no" has to stop the request, not just delay it.

    This is the half of a confirm that a markup assertion structurally cannot see. Reading
    ``hx-confirm="Enqueue metadata extraction for all pending files?"`` off the button proves the
    prompt exists; it says nothing about what htmx does with the answer. The consequence of getting
    it wrong is not cosmetic -- Extract All fans one SAQ job out per eligible file, so a confirm that
    is displayed but not honoured is a bulk enqueue the operator explicitly declined.

    Three files are seeded so the control is acting on a real, non-empty selection: a dismissed
    confirm over an empty set would pass this test even if the boundary were broken.
    """
    for index in range(3):
        await seed.file(filename=f"<track-{index + 1:02d}>.mp3")

    await open_shell(page, "/s/metadata")
    await settled(page)
    posted = _watch_extract_posts(page)

    dismissed: list[str] = []

    # An `async def` handler, not a lambda. `dialog.dismiss()` is a coroutine, and a lambda that
    # does anything besides return it (recording the message, say) hands pyee a value it cannot
    # await -- the dismissal is then never performed and Playwright's own auto-dismiss decides the
    # outcome instead of this test. Seen as "coroutine Dialog.dismiss was never awaited".
    async def _dismiss(dialog: Any) -> None:
        dismissed.append(dialog.message)
        await dialog.dismiss()

    page.on("dialog", _dismiss)

    await page.click(_EXTRACT)
    await settled(page)
    # Nothing to wait FOR when the contract holds, so give a request that should not exist time to
    # be made. Short, and only ever paid by this one test.
    await page.wait_for_timeout(1000)

    assert dismissed, "Extract All fired no confirmation at all — a bulk enqueue with no boundary"
    assert "Enqueue metadata extraction for all pending files?" in dismissed[0], f"the confirm does not name what it is about to do: {dismissed[0]!r}"
    assert posted == [], f"a DISMISSED confirm still enqueued metadata extraction: {posted}"
    assert (await page.locator("#metadata-trigger-response").inner_text()).strip() == "", "the ack region reported an enqueue the operator declined"


async def test_accepting_the_extract_confirm_posts_once_and_answers_in_the_live_region(page: Any, seed: Any) -> None:
    """The other half: accepting sends exactly one request and reports back where a reader hears it.

    ``#metadata-trigger-response`` is ``aria-live="polite"``, which is the entire accessibility story
    for this action -- the button gives no other feedback, and the ack replaces the region's content
    rather than moving focus. A live region only announces if the swap lands INSIDE it, so the target
    and the region have to be the same element in a live document; two fragments that merely both
    mention ``metadata-trigger-response`` cannot show that.

    "Exactly one" matters because ``hx-confirm`` re-issues the request after the dialog resolves: a
    mis-wired confirm double-fires, which for a bulk enqueue means two batches.
    """
    for index in range(3):
        await seed.file(filename=f"<track-{index + 1:02d}>.mp3")

    await open_shell(page, "/s/metadata")
    await settled(page)
    posted = _watch_extract_posts(page)

    page.on("dialog", lambda dialog: dialog.accept())

    await page.click(_EXTRACT)
    await settled(page)
    await page.wait_for_function("() => document.querySelector('#metadata-trigger-response').innerText.trim() !== ''")

    assert len(posted) == 1, f"Extract All sent {len(posted)} requests for one accepted confirm"
    region = page.locator("#metadata-trigger-response")
    assert await region.get_attribute("aria-live") == "polite", "the enqueue ack lands somewhere no screen reader is listening"
    assert "metadata extraction" in await region.inner_text(), (
        f"the ack does not name the action it is acknowledging: {(await region.inner_text()).strip()!r}"
    )


async def test_the_eligible_files_view_loads_itself_after_a_rail_swap(page: Any, seed: Any) -> None:
    """``hx-trigger="load"`` inside a SWAPPED fragment, which is where that trigger goes wrong.

    ``#metadata-files-view`` ships empty with a "Loading eligible files" placeholder and fetches its
    own contents. On a direct navigation that is uninteresting -- htmx processes the whole document.
    Arriving by a rail click is the case that can break: the workspace is innerHTML-swapped into
    ``#stage-workspace``, and the deferred fetch fires only if htmx processed the swapped-in subtree.
    When it does not, the panel sits on its placeholder forever and the operator is told the eligible
    set is loading rather than that it is empty or full. The server renders byte-identical HTML in
    both cases, so only a browser that took the rail route can tell them apart.

    Asserting the seeded filenames (not just "the placeholder went away") is what makes this a
    content test rather than a spinner test.
    """
    await seed.file(filename="<track-01>.mp3")
    await seed.file(filename="<track-02>.m4a", file_type="m4a")

    await open_shell(page, "/s/summary")
    await settled(page)

    await click_swap(page, 'a[data-rail-stage="metadata"]')
    await wait_for_stage(page, "Metadata")
    await settled(page)

    view = page.locator("#metadata-files-view")
    await page.wait_for_function("() => !document.querySelector('#metadata-files-view').innerText.includes('Checking the metadata queue')")
    body = await view.inner_text()
    assert "<track-01>.mp3" in body, f"the eligible-files view never fetched itself after the rail swap: {body[:200]!r}"
    assert "<track-02>.m4a" in body, "the eligible-files view rendered a partial selection"


async def test_the_queue_controls_are_disabled_and_explained_from_the_live_store(page: Any, seed: Any) -> None:
    """A disabled control and the sentence explaining it, both computed in the browser.

    PAUSE is bound to ``:disabled="$store.pipeline.metadataBusy === 0"`` and EXTRACT ALL to the same
    key in the opposite direction, so exactly one of the pair is ever offered. Server-side both are
    literal attribute text on elements that are always rendered; the boolean exists only once Alpine
    has evaluated the store. A markup test can prove the binding was written and never that it
    resolved -- which is how a store key gets renamed on one side and a control silently becomes
    permanently enabled, or permanently dead, with the template still reading correct.

    Driving the store directly rather than asserting whatever it happens to hold, because it does
    NOT hold a fixed value: ``metadataBusy`` is SAQ queue depth, ``saq_jobs`` is on ``seed.reset``'s
    preserved-tables list (it is the running server's own state), and an earlier test in this file
    really does enqueue jobs no worker will ever drain. A "the queue is idle" assertion would
    therefore pass or fail on test ORDER. Writing both values proves the thing actually at issue --
    that each binding tracks that key, in both directions -- and proves it deterministically.

    The eligible metric is asserted as rendered because it IS deterministic: it comes from ``files``,
    which ``reset`` truncates.
    """
    for index in range(3):
        await seed.file(filename=f"<track-{index + 1:02d}>.mp3")

    await open_shell(page, "/s/metadata")
    await settled(page)

    assert await page.locator("#metadata-eligible-value").inner_text() == "3", "the eligible metric did not resolve against the seeded files"

    extract = page.locator(_EXTRACT)

    await page.evaluate("() => { Alpine.store('pipeline').metadataBusy = 0; }")
    await page.wait_for_function("() => document.querySelector('button[aria-label=\"Pause metadata\"]').disabled === true")
    assert not await extract.is_disabled(), "EXTRACT ALL is disabled on an idle queue — the workspace offers no action at all"

    body = await page.locator("#stage-workspace").inner_text()
    assert "Extract All currently selects no files" not in body, (
        "the idle explanation claims Extract All selects nothing while three files are eligible"
    )

    await page.evaluate("() => { Alpine.store('pipeline').metadataBusy = 4; }")
    await page.wait_for_function("() => document.querySelector('button[aria-label=\"Pause metadata\"]').disabled === false")
    assert await extract.is_disabled(), (
        "EXTRACT ALL stays clickable with metadata jobs in flight — the double-enqueue guard is not wired to the store"
    )
