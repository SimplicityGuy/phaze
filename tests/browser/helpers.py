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

from typing import Any


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


async def settled(page: Any) -> None:
    """Wait until htmx has no request in flight.

    The honest alternative to sprinkling ``wait_for_timeout``. htmx increments
    ``document.body`` request counters around every swap; polling for zero waits exactly as long as
    the interaction takes instead of a guessed constant, which is the difference between a test that
    is slow and a test that is flaky. Callers still assert on a specific post-condition afterwards
    -- this only establishes that the network step finished.
    """
    await page.wait_for_function("() => document.querySelectorAll('.htmx-request').length === 0")
