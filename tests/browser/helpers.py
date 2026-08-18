"""Shared navigation/readiness helpers for the seeded browser tests (phaze-8p1uq).

``test_shell_contract.py`` grew private copies of ``_open`` / ``_wait_for_stage`` when it was the
only file in this suite. It is not any more, and the readiness rules those helpers encode are the
non-obvious ones -- the ``networkidle`` trap and the ``data-stage`` trap each cost a run to find --
so they belong somewhere a new test file can reach them instead of being rediscovered.

That file is deliberately left alone: it is ``phaze-tzy6s.14``'s deliverable, its copies are
correct, and rewriting it to import from here would be churn on a file this bead was told to
extend rather than replace.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


async def open_shell(page: Any, path: str = "/s/summary") -> None:
    """Navigate and wait until the shell is actually interactive.

    Deliberately NOT ``wait_until="networkidle"``. The shell holds a 5s stats poll and opens SSE
    streams, so the network is never idle and that wait blocks until the timeout.
    """
    await page.goto(path, wait_until="domcontentloaded")
    await page.wait_for_selector("#stage-workspace", state="attached")
    await page.wait_for_function("() => window.Alpine !== undefined && window.htmx !== undefined")


async def wait_for_stage(page: Any, document_title: str) -> None:
    """Wait for an htmx rail swap to land, via the fragment's own ``data-document-title`` marker.

    NOT ``#stage-workspace``'s ``data-stage``: the rail swaps with ``hx-swap="innerHTML"``, so the
    container's attributes survive frozen at whatever the initial full-page render set.
    """
    await page.wait_for_function(
        """expected => {
            const marker = document.querySelector('#stage-workspace [data-document-title]');
            return marker !== null && marker.dataset.documentTitle === expected;
        }""",
        arg=document_title,
    )


_SWAP_COUNTER = """() => {
    if (!window.__phazeSwapCounterArmed) {
        window.__phazeSwapCounterArmed = true;
        window.__phazeSwaps = 0;
        document.body.addEventListener('htmx:afterSettle', () => { window.__phazeSwaps++; });
    }
    return window.__phazeSwaps;
}"""


@contextlib.asynccontextmanager
async def swap_settles(page: Any) -> AsyncGenerator[None]:
    """Wrap an interaction that triggers an htmx swap; exit once that swap has SETTLED.

    ``settled()`` is the wrong barrier immediately after an interaction: it polls for zero in-flight
    requests, and right after ``page.click()`` returns there are usually none yet -- the request has
    not started -- so it passes instantly and guarantees nothing. Waiting on a post-condition in the
    swapped content is better but still lands one phase early, because htmx renders and then runs a
    separate SETTLE phase in which it finishes the node's attribute transitions and does its
    ``hx-push-url`` history bookkeeping.

    Driving a SECOND interaction inside that window is what this exists to avoid. Measured on the
    Audit filter tabs, which swap the container the tabs themselves live in: clicking the next tab the
    instant the previous tab's ``aria-pressed`` flipped lost the second click entirely -- no request
    was ever issued, the URL stayed on the first filter, and the test timed out. It reproduced 4 times
    in 6 isolated runs, and every attempt to observe it made it disappear, because each extra
    round-trip an instrumenting call adds is enough delay to close the window. That is the signature
    of a settle-phase race in the TEST, not a product defect: driven at operator speed, or with any
    pause at all, the tabs work correctly every time. With this barrier the same file ran 8 for 8.

    ``htmx:afterSettle`` is the event that says the phase is over, so counting it is the barrier the
    mechanism actually provides. A retry around the click would have hidden the same race behind a
    green run.
    """
    before = await page.evaluate(_SWAP_COUNTER)
    yield
    await page.wait_for_function("n => window.__phazeSwaps > n", arg=before)


async def click_swap(page: Any, selector: str) -> None:
    """Click an htmx control and wait for its swap to settle -- :func:`swap_settles` for one click.

    Read that context manager's docstring for why a settle barrier is the right one; this is the
    common case, where the interaction is a single click.
    """
    async with swap_settles(page):
        await page.click(selector)


async def settled_focus(page: Any, describe: str = "id", *, timeout_ms: int = 5_000) -> str:
    """Wait until focus stops moving, then return how the focused element describes itself.

    Every dismiss path in this app restores focus from an Alpine ``$nextTick`` -- deliberately, and
    with a comment saying why: a synchronous ``focus()`` is undone when the browser resets focus to
    ``<body>`` as the hidden panel's focused child is removed on the same tick. The consequence for a
    test is that the *observable* post-condition of a dismiss (the panel is gone) lands one tick
    BEFORE the focus restore, so reading ``document.activeElement`` immediately after it is a race
    that passes on a fast machine and fails on a slow one.

    Waiting for stability rather than for a specific expected id keeps the failure message useful:
    the caller still asserts, and still gets to say what it wanted and what it got, instead of a bare
    selector timeout.

    ``describe`` names the property to read back -- ``"id"`` or an attribute like ``"aria-label"``.
    """
    read = "el.id" if describe == "id" else f"el.getAttribute({describe!r})"
    script = f"""() => {{
        const el = document.activeElement;
        return el ? ({read} || '') : '';
    }}"""

    # An empty read means focus is on <body> (or an element with no such attribute), which is the
    # FAILURE this helper exists to report rather than a state to settle on -- so it never counts
    # towards stability. A genuinely broken restore therefore spends the full timeout and returns "",
    # and the caller's assertion says so plainly.
    previous = await page.evaluate(script)
    stable = 0
    for _ in range(max(1, timeout_ms // 100)):
        await page.wait_for_timeout(100)
        current = await page.evaluate(script)
        stable = stable + 1 if current == previous and current else 0
        if stable >= 2:
            return str(current)
        previous = current
    return str(previous)


async def settled(page: Any) -> None:
    """Wait until htmx has no request in flight.

    The honest alternative to sprinkling ``wait_for_timeout``. htmx increments
    ``document.body`` request counters around every swap; polling for zero waits exactly as long as
    the interaction takes instead of a guessed constant, which is the difference between a test that
    is slow and a test that is flaky. Callers still assert on a specific post-condition afterwards
    -- this only establishes that the network step finished.
    """
    await page.wait_for_function("() => document.querySelectorAll('.htmx-request').length === 0")
