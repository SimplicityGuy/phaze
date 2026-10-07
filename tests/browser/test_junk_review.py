"""phaze-l1j35: the junk review's core approve-then-run flow, and its 409 refusal, in a real browser.

The server-side tests (``tests/review/routers/test_junk_review.py``) prove the decisions and the
token. What they cannot prove is the half that lives in htmx: that the card's ``outerHTML`` swap
lands in place of the card, that the outcome reaches the polite live region OUT of band, that focus
follows the swap to the refreshed card, and that a 409 -- which htmx does NOT swap by default -- is
shown, because shell.html's ``htmx:beforeSwap`` opts ``data-conflict-swap`` targets in.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.models.companion_junk_review import CompanionJunkReview
from tests.browser.helpers import click_swap, open_shell


pytestmark = pytest.mark.browser


async def test_approve_then_run_swaps_the_card_and_announces_each_outcome(page: Any, seed: Any) -> None:
    """Approve marks the group (undoable); "Quarantine approved" is the separate run step.

    The two-step shape is the operator's 2026-10-07 amendment recorded on phaze-l1j35 ("Approve, then a
    separate Run step").
    """
    rows = await seed.junk_group(count=2)
    sha = rows[0].sha256_hash
    card = f"#junk-group-{sha}"

    await open_shell(page, "/s/junk")
    await page.evaluate("window.__documentAlive = true")

    await click_swap(page, f"{card} button:text-is('Approve')")

    status = await page.locator("#junk-review-status").inner_text()
    assert "Approved 2 copies" in status, f"the outcome never reached the live region: {status!r}"
    assert await page.locator(card).count() == 1, "the card swap duplicated or dropped the card"
    assert await page.locator(f"{card} button:text-is('Approve')").count() == 0, "the approved card still offers Approve"
    assert await page.locator(f"{card} button:text-is('Undo')").count() == 1, "an approval that has not run must be undoable"
    focused = await page.evaluate("document.activeElement && document.activeElement.id")
    assert focused == f"junk-group-{sha}-title", f"focus did not follow the swap to the refreshed card (on {focused!r})"

    await click_swap(page, f"{card} button:text-is('Quarantine approved')")

    status = await page.locator("#junk-review-status").inner_text()
    assert "of 2 copies approved" in status, f"the run's outcome never reached the live region: {status!r}"
    assert await page.evaluate("window.__documentAlive === true"), "a decision reloaded the document"


async def test_a_group_that_changed_after_render_is_refused_and_redrawn(page: Any, seed: Any) -> None:
    rows = await seed.junk_group(count=2)
    sha = rows[0].sha256_hash
    card = f"#junk-group-{sha}"

    await open_shell(page, "/s/junk")
    # A third identical copy is detected after the operator loaded the page.
    extra = await seed.file(filename="<set-03>-info.nfo", sha256=sha, file_type="nfo", file_size=96)
    seed.session.add(
        CompanionJunkReview(
            agent_id=extra.agent_id,
            original_path=extra.original_path,
            sha256_hash=sha,
            file_id=extra.id,
            file_type="nfo",
            file_size=96,
            reason="known_stamp",
            content_group=sha,
        )
    )
    await seed.session.commit()

    await click_swap(page, f"{card} button:text-is('Approve')")

    status = await page.locator("#junk-review-status").inner_text()
    assert "nothing was applied" in status, f"the 409 refusal was not shown: {status!r}"
    body = await page.locator(card).text_content() or ""
    assert "3 pending" in body, f"the refused card was not redrawn with what is really there: {body[:300]!r}"
    assert await page.locator(f"{card} button:text-is('Approve')").count() == 1, "the redrawn card lost its Approve control"
