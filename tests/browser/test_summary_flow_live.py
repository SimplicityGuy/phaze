"""phaze-2dv18: every figure on the Summary Pipeline Flow card moves with the shell poll, with no reload.

The sidebar and Recent Activity bind to ``$store.pipeline`` and advance on the 5s ``/pipeline/stats`` tick; the
flow card used to print the same quantities as plain server-rendered text, so while analyses completed the three
disagreed (Analyze 8,998 on the card against 9,016 beside it) until the operator refreshed. Server-side tests can
prove the template carries a binding; only a browser can prove the card's text actually changes after a tick.
Seeding happens AFTER the page has rendered, so only the poll can account for the new numbers.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.proposal import ProposalStatus
from tests.browser.helpers import open_shell


pytestmark = pytest.mark.browser


# The shell polls every 5s; wait generously relative to that rather than tuning to a tick.
_POLL_WINDOW_MS = 20_000


async def _card_text(page: Any, name: str) -> str:
    return " ".join((await page.locator(f'[data-summary-overview] [data-flow-card="{name}"]').inner_text()).split())


async def _wait_text(page: Any, selector: str, expected: str) -> None:
    await page.wait_for_function(
        "([sel, want]) => document.querySelector(sel).textContent.replace(/\\s+/g, ' ').includes(want)",
        arg=[selector, expected],
        timeout=5_000,
    )


async def test_the_flow_card_counts_advance_with_the_poll_and_agree_with_the_sidebar(page: Any, seed: Any) -> None:
    first = await seed.file(filename="<set-01>.mp3")
    await seed.file(filename="<set-02>.mp3")
    await seed.analysis(first)
    await open_shell(page, "/s/summary")

    assert "1 / 2 files" in await _card_text(page, "Analyze")
    assert "2 files" in await _card_text(page, "Discover")
    assert "0 awaiting decision" in await _card_text(page, "Review")
    assert "0 files with tracklists" in await _card_text(page, "Tracklists")

    # Now the corpus moves with no interaction of any kind: one more file discovered, a second file analyzed,
    # and proposals appear in the two proposal-row states the Review and Execute cards count (each proposal seeds its
    # own file, so the corpus ends at five).
    third = await seed.file(filename="<set-03>.mp3")
    await seed.analysis(third)
    await seed.tracklist(file=first)
    await seed.proposal(status=ProposalStatus.PENDING, filename="<set-04>.mp3")
    await seed.proposal(status=ProposalStatus.APPROVED, filename="<set-05>.mp3")

    await page.wait_for_function("() => Alpine.store('pipeline').analyzeDone === 2", timeout=_POLL_WINDOW_MS)

    # The store (what the sidebar reads) has moved; the card must have moved with it, in the same tick.
    card = '[data-summary-overview] [data-flow-card="{}"]'
    await _wait_text(page, card.format("Analyze"), "2 / 5 files")
    assert "5 files" in await _card_text(page, "Discover")
    assert "1 awaiting decision" in await _card_text(page, "Review")
    assert "1 approved now" in await _card_text(page, "Execute")
    assert page.url.endswith("/s/summary"), "the live refresh navigated -- that is a reload, not a refresh"

    # The board's SVG node labels and the screen-reader text list carry the same figures and move with them.
    await _wait_text(page, '[data-flow-node="Analyze"]', "2 / 5")
    await _wait_text(page, '[data-flow-text-node="Analyze"]', "2 of 5 files")
    await _wait_text(page, '[data-flow-text-edge="analyze-join"]', "2 files analyzed")
    await _wait_text(page, card.format("Tracklists"), "1 files with tracklists")
    await _wait_text(page, '[data-flow-node="Tracklists"]', "1 files")
    await _wait_text(page, '[data-flow-text-node="Tracklists"]', "1 files with tracklists")
    # The same null that a degraded read publishes must update availability everywhere, then recover.
    for count, label in [(None, "unavailable"), (1, "optional")]:
        await page.evaluate("count => { Alpine.store('pipeline').summaryFlow.tracklists = count; }", count)
        for selector in (card.format("Tracklists"), '[data-flow-node="Tracklists"]', '[data-flow-text-node="Tracklists"]'):
            await _wait_text(page, selector, label)
    await page.locator(card.format("Tracklists")).click()
    await page.wait_for_url("**/s/tracklist")
    await page.wait_for_selector("[data-summary-overview]", state="detached")
