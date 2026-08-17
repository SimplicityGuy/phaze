"""phaze-8p1uq: a REAL execute dispatch, and the SSE stream it opens.

``phaze-tzy6s.14`` could only reach the disabled branch of this control. Its own docstring says so
at length (``test_execute_always_states_the_scope_it_will_not_dispatch``) and correctly declines to
assert anything about the confirmation boundary it could not reach: ``apply_workspace.html`` gates
``#apply-confirm`` behind ``preflight.can_execute``, which needs approved proposals, which needs
seeding. With seeding, the whole path opens up -- the in-product dialog, the dispatch, the progress
card, and the SSE stream whose close semantics are the phaze-047gd family.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.browser.helpers import open_shell, settled


pytestmark = pytest.mark.browser


# EventSource's spec-default reconnection delay is ~3s; Chromium uses that. A stream that was
# supposed to close, but wasn't, therefore re-requests within this window. Anything shorter proves
# nothing (the reconnect simply hasn't fired yet), which is how a vacuous version of the assertion
# below would look identical to a passing one.
_RECONNECT_WINDOW_SEC = 6.0


async def _wait_for_stream_request(seen: list[str], *, timeout_sec: float = 15.0) -> None:
    """Block until the EventSource has actually issued its request.

    Readiness is taken from the NETWORK, not from the card's text. The progress card is fully
    server-rendered on first response -- counters, per-agent table and all -- so every visible
    string in it is present before the stream connects, and waiting on any of them would return
    immediately whether or not an EventSource was ever created. The request is the only observable
    that distinguishes the two.
    """
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while not seen:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no request to /execution/progress/ within {timeout_sec:.0f}s — the progress card opened no stream")
        await asyncio.sleep(0.1)


async def _batch_id(page: Any) -> str:
    """The batch id the just-rendered progress card opened its stream against."""
    connect = await page.evaluate("document.querySelector('#apply-execute-response [sse-connect]')?.getAttribute('sse-connect')")
    assert connect, "the progress card carries no sse-connect — no stream was opened"
    return str(connect).rsplit("/", 1)[-1]


async def _dispatch(page: Any) -> None:
    """Drive Execute Approved through its confirmation dialog, leaving the progress card mounted."""
    trigger = page.locator('button[aria-label^="Review and execute"]')
    assert not await trigger.is_disabled(), "Execute is disabled with approved proposals seeded — preflight.can_execute is False"
    await trigger.click()

    dialog = page.locator("#apply-confirm")
    await dialog.wait_for(state="visible")
    await dialog.locator("button:has-text('Execute')").click()
    await page.wait_for_selector("#apply-execute-response [sse-connect]", timeout=30_000)


async def test_execute_approved_dispatches_only_through_the_in_product_dialog(page: Any, seed: Any) -> None:
    """Execute opens the in-product ``<dialog>``, no native prompt fires, and confirming dispatches.

    This is the browser-level version ``test_shell_contract.py`` explicitly deferred to this bead.
    The native-prompt half is not hypothetical bookkeeping: ``hx-confirm`` renders as a blocking
    ``window.confirm``, which cannot be styled, cannot state the excluded-item counts this product's
    confirmation is built around, and -- because Playwright auto-dismisses unhandled dialogs -- would
    make a test of the confirm path silently assert the CANCEL path instead. Listening for native
    dialogs and asserting none appeared is what distinguishes "the right dialog opened" from "some
    dialog opened".

    Seeding two proposals rather than one is deliberate: the dialog's title is pluralised from
    ``preflight.total``, so a single row would let an off-by-one in the count read as correct.
    """
    await seed.approved_proposals(2)

    native_dialogs: list[str] = []
    page.on("dialog", lambda dialog: native_dialogs.append(dialog.type))

    await open_shell(page, "/s/apply")

    confirm = page.locator("#apply-confirm")
    assert not await confirm.is_visible(), "the confirmation dialog is open before Execute was pressed"

    await page.locator('button[aria-label^="Review and execute"]').click()
    await confirm.wait_for(state="visible")
    assert native_dialogs == [], f"a native browser dialog fired ({native_dialogs}) — the confirmation must be the in-product <dialog>"
    assert "2" in await confirm.locator("h2").inner_text(), "the dialog does not state how many operations it will run"

    await confirm.locator("button:has-text('Execute')").click()
    await page.wait_for_selector("#apply-execute-response [sse-connect]", timeout=30_000)

    assert native_dialogs == [], f"a native browser dialog fired during dispatch ({native_dialogs})"
    assert not await confirm.is_visible(), "the dialog stayed open after a successful dispatch"

    summary = await page.locator("#apply-execute-response").inner_text()
    assert "Dispatched 2 proposals" in summary, f"the progress card does not report the dispatch it just made: {summary[:200]!r}"


async def test_cancelling_the_confirmation_dispatches_nothing(page: Any, seed: Any) -> None:
    """Cancel closes the dialog and leaves the corpus untouched -- the boundary's other half.

    A confirmation that cannot be declined is not a confirmation. The assertion that matters is the
    absence of a progress card: ``/execution/start`` is not idempotent (each POST mints a new
    ``batch_id`` and re-enqueues every still-APPROVED row -- phaze-fa2p), so a Cancel button wired
    to the wrong form would dispatch the entire approved set with no way to take it back.
    """
    await seed.approved_proposals(2)
    await open_shell(page, "/s/apply")

    await page.locator('button[aria-label^="Review and execute"]').click()
    confirm = page.locator("#apply-confirm")
    await confirm.wait_for(state="visible")

    await confirm.locator("button:has-text('Cancel')").click()
    await confirm.wait_for(state="hidden")
    await settled(page)

    assert await page.locator("#apply-execute-response [sse-connect]").count() == 0, "Cancel dispatched the batch anyway"
    assert (await page.locator("#apply-execute-response").inner_text()).strip() == "", "Cancel left a response in the dispatch target"


async def test_the_progress_stream_opens_once_and_stops_reconnecting_after_close(page: Any, seed: Any, redis_url: str) -> None:
    """The phaze-047gd contract, asserted where it actually lives: the browser's EventSource.

    ``progress.html`` carries ``sse-close="close"`` on the same element as ``sse-connect`` because
    htmx-ext-sse@2.2.4 reads ``sse-close`` from nowhere else, and ``execution_progress`` emits a
    canonical ``{"event": "close"}`` from both terminal paths for that listener to catch. Every
    server-side test can prove is that the attribute is written and the event is emitted. Neither
    fact is the bug. The bug is what the BROWSER does when the server's generator returns: an
    EventSource whose close listener never fired treats the ended stream as a dropped connection and
    re-requests it roughly every three seconds, forever, against a batch that is already finished.

    So the assertion is a request count across a full reconnect window, not a DOM state. Counting
    requests is what makes it fail correctly if ``sse-close`` is moved back onto a descendant span:
    the terminal text still renders (that arrives on the status-named event, which does swap), the
    card still looks right, and only the network shows the leak.

    Redis is written directly to reach the terminal state. There is no operator gesture that
    completes an execution -- a SAQ worker reports sub-job completion and a Lua script promotes the
    hash to ``complete`` -- and running a worker inside a browser test would be testing the worker.
    The hash IS the stream's whole input, so writing it exercises the same code path the promotion
    script drives.
    """
    from redis.asyncio import Redis

    await seed.approved_proposals(2)

    stream_requests: list[str] = []
    page.on("request", lambda request: stream_requests.append(request.url) if "/execution/progress/" in request.url else None)

    await open_shell(page, "/s/apply")
    await _dispatch(page)
    batch_id = await _batch_id(page)

    # The stream was opened -- exactly once -- before anything is asked of its close path.
    await _wait_for_stream_request(stream_requests)
    assert len(stream_requests) == 1, f"expected exactly one stream connection, saw {len(stream_requests)}"

    redis_client = Redis.from_url(redis_url, decode_responses=True)
    try:
        assert await redis_client.exists(f"exec:{batch_id}"), f"the dispatch seeded no exec:{batch_id} hash — nothing was really dispatched"
        await redis_client.hset(f"exec:{batch_id}", mapping={"completed": "2", "failed": "0", "status": "complete"})
    finally:
        await redis_client.aclose()

    # The terminal event lands: the card says so on screen.
    await page.wait_for_function(
        """() => (document.querySelector('#apply-execute-response')?.textContent || '').includes('Execution complete')""",
        timeout=15_000,
    )

    # ...and, the actual contract, the browser stops asking. A stream left open re-requests within
    # the reconnect window; one that honoured sse-close never does.
    await asyncio.sleep(_RECONNECT_WINDOW_SEC)
    assert len(stream_requests) == 1, (
        f"the EventSource reconnected {len(stream_requests) - 1} time(s) after the terminal close event — "
        "sse-close did not close the stream (phaze-047gd)"
    )


async def test_an_execution_that_finishes_with_failures_reports_them_and_still_closes(page: Any, seed: Any, redis_url: str) -> None:
    """The ERROR state of the live surface: ``complete_with_errors`` must terminate like ``complete``.

    The two terminal statuses are checked by one ``status in {...}`` set, and a set is exactly the
    thing a refactor narrows by accident. Narrowed to ``complete`` alone, a batch with any failure
    never emits its close event and every partially-failed execution leaks a reconnecting stream --
    the worst-affected case being the one an operator is most likely to leave open on screen while
    working out what went wrong.
    """
    from redis.asyncio import Redis

    await seed.approved_proposals(3)
    stream_requests: list[str] = []
    page.on("request", lambda request: stream_requests.append(request.url) if "/execution/progress/" in request.url else None)

    await open_shell(page, "/s/apply")
    await _dispatch(page)
    batch_id = await _batch_id(page)

    redis_client = Redis.from_url(redis_url, decode_responses=True)
    try:
        await redis_client.hset(f"exec:{batch_id}", mapping={"completed": "2", "failed": "1", "status": "complete_with_errors"})
    finally:
        await redis_client.aclose()

    await page.wait_for_function(
        """() => (document.querySelector('#apply-execute-response')?.textContent || '').includes('2 succeeded, 1 failed')""",
        timeout=15_000,
    )

    await asyncio.sleep(_RECONNECT_WINDOW_SEC)
    assert len(stream_requests) == 1, (
        f"complete_with_errors left the stream reconnecting ({len(stream_requests)} requests) — only 'complete' closes it"
    )
