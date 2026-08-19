"""Wait for an in-process condition to settle, or RAISE -- never give up quietly (phaze-5lq8a).

This is the second instance of the antipattern that bead removed from the background-task drains,
kept separate because the two wait for genuinely different things:
:func:`tests._background_drain.drain_router_background_tasks` awaits a known set of TASKS, while this
polls a PREDICATE nothing hands you a handle to (a mock's ``await_count``, a counter a patched
callable increments, a log record appearing in ``caplog``).

The shape it replaces::

    async def _wait_until(predicate, *, timeout=2.0):
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                return          # <- silently gives up and lets the caller carry on
            await asyncio.sleep(0)

THREE byte-identical copies of that lived under ``tests/shared/tasks/`` --
``test_heartbeat_hang.py``, ``test_heartbeat_broker_unavailable.py`` and ``test_heartbeat_loop.py``.
The third was found by ``tests/shared/test_settle_discipline.py`` after a hand grep had reported
two; ``tests/browser/conftest.py::_wait_until_serving`` is a fourth helper of the same NAME shape
that already raises on its deadline, and is correct as it stands.

WHAT THIS DID AND DID NOT COST, checked rather than assumed
-----------------------------------------------------------

Every one of the four call sites re-asserts the same condition immediately after waiting, so a
silent give-up was already caught -- as ``assert 2 >= 3``, from a line that reads like the
production behaviour failed rather than like the test stopped waiting too early. Raising here
therefore changes no test's pass/fail outcome; it changes what the failure SAYS. That distinction
is the whole reason to make the change: the shape is wrong even where a downstream assertion
happens to cover for it, and the next copy may not be so lucky.

``asyncio.sleep(0)`` is retained deliberately, and is not the defect the drains had. There the
awaited work was a database round-trip, so yielding could never advance it; here the awaited work is
a task on this same loop driving mocked I/O, so a bare yield IS the thing that lets it run, and it
settles in microseconds.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0, description: str | None = None) -> None:
    """Poll ``predicate`` until it is true, or raise ``AssertionError`` once ``timeout`` elapses.

    ``description`` names the awaited condition in the failure, because every caller passes a
    lambda and ``repr`` of one says nothing useful.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            what = description or "the awaited condition"
            raise AssertionError(
                f"{what} was still false after {timeout:g}s. Returning here instead would let the assertions below "
                "run against a state that never settled, and report the miss as though the behaviour under test had "
                "failed (phaze-5lq8a)."
            )
        await asyncio.sleep(0)
