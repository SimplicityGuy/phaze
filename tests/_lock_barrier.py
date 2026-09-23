"""Wait until a racing task is observably queued on a Postgres lock -- on the CONDITION, not the clock (phaze-lz69g).

The real-PG concurrency tests (``tests/integration/test_scan_reaper_concurrency.py``,
``test_scan_deletion_concurrency.py``, ``test_stage_pause_resume_lock.py``) all race the same way: one
session holds a row lock, a contender is launched on its own connection, and the holder releases only
once the contender is *observed* blocked behind it. That observation is the barrier.

THE SHAPE THIS REPLACES
-----------------------

Three byte-identical copies of::

    async def _wait_for_blocked_waiter(session_factory, *, timeout=5.0):
        async def _poll():
            while True:
                async with session_factory() as probe:          # a NEW connection every iteration
                    waiting = (await probe.execute(text(BLOCKED_WAITER_SQL))).scalar()
                if waiting:
                    return
                await asyncio.sleep(0.02)
        await asyncio.wait_for(_poll(), timeout=timeout)

It failed intermittently whenever several seats' suites shared the ``phaze-test-db`` container
(phaze-lz69g, and the bead's comments for the two sibling files), for two compounding reasons:

* **The budget measured the machine, not the race.** 5 s was chosen on a quiet machine. Under 7-9
  concurrent suites the contender's own connect + statement can take longer than that to reach the
  server and queue, so the barrier expired while everything was progressing correctly.
* **The budget was spent on things the barrier was not observing.** Every poll opened a fresh pool
  connection -- and the first one paid ``getaddrinfo('localhost')`` through the default executor --
  inside the deadline. Two recorded failures were ``CancelledError`` raised mid-``getaddrinfo`` in the
  PROBE, before any SQL was sent.

And both failure modes surfaced as the same undifferentiated red a genuine regression would.

WHAT THIS DOES INSTEAD
----------------------

* **No fixed deadline on the normal path.** There are exactly two legitimate outcomes, and both are
  conditions, not durations: the contender is observed queued (return), or the contender FINISHED
  without ever being observed queued (raise -- it never contended for the held lock, which for a
  lock-serialization test is itself the regression). A slow-but-progressing machine therefore waits
  rather than fails, however loaded it is.
* **One probe connection, opened before polling starts, in AUTOCOMMIT.** The connect is paid once and
  is not charged against anything. AUTOCOMMIT is load-bearing, not cosmetic: ``pg_stat_activity`` is
  snapshotted per TRANSACTION, so a probe that held one transaction across polls would keep reading a
  snapshot taken before the contender connected and never see it. That snapshot is only taken once the
  join actually scans ``pg_stat_activity``, which on a quiet cluster (no ungranted lock anywhere) it
  does not -- but under multi-seat load a SIBLING seat's waiter makes it scan on the first poll. So a
  single-transaction probe passes on an idle machine and hangs under exactly the contention this module
  exists for; ``tests/integration/test_lock_barrier.py`` reproduces that with a sibling-database waiter.
  Each statement here is its own transaction, so every poll reads the live catalogue.
* **A ceiling that is hang protection, not a tuning knob.** The only way to reach it is a contender
  that is neither queued on a lock nor finishing -- wedged. It is set well above asyncpg's own 60 s
  connect timeout (the slowest legitimate step a contender takes before it can queue), so a slow
  connect surfaces as the CONTENDER's own error through the "finished" branch rather than as a barrier
  expiry. It is not "the next number that happened to pass": no green or red outcome in these tests
  depends on its value.
* **Distinguishable failures.** Every barrier failure raises :class:`LockBarrierError`, whose message
  opens with ``LOCK BARRIER`` and says in words that the barrier -- not the assertion under test --
  is what failed. A genuine race failure is the calling test's own ``assert`` with its own message.

The probe SQL is :data:`tests.db_guard.BLOCKED_WAITER_SQL`, scoped to ``current_database()`` -- the
cluster-wide-catalogue rule in ``CLAUDE.md`` applies unchanged.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import text

from tests.db_guard import BLOCKED_WAITER_SQL


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


# Hang protection only -- see the module docstring. asyncpg's default connect timeout is 60 s; five of
# those leaves a contender room to pay a slow connect and still be reported by the "finished" branch.
WEDGED_CONTENDER_CEILING_SEC = 300.0

POLL_INTERVAL_SEC = 0.02


class LockBarrierError(AssertionError):
    """The lock barrier failed -- NOT the behaviour the calling test asserts on.

    Subclasses ``AssertionError`` so pytest reports it as a test failure rather than an error, while
    the ``LOCK BARRIER`` prefix keeps it distinguishable from the test's own assertions at a glance.
    """


def _contender_outcome(contender: asyncio.Task[Any]) -> str:
    if contender.cancelled():
        return "it was cancelled"
    exc = contender.exception()
    if exc is not None:
        return f"it raised {exc!r}"
    return f"it returned {contender.result()!r}"


async def wait_for_blocked_waiter(
    session_factory: async_sessionmaker[AsyncSession],
    contender: asyncio.Task[Any],
    *,
    ceiling: float = WEDGED_CONTENDER_CEILING_SEC,
) -> None:
    """Return once some backend IN THIS DATABASE is queued on a lock; raise if ``contender`` ends first.

    ``contender`` is the task expected to block behind the caller's held lock. It is watched, never
    awaited or cancelled here: on failure the caller still owns it (release the held lock, then await
    it) so it is not left pending.
    """
    engine: AsyncEngine = session_factory.kw["bind"]
    loop = asyncio.get_running_loop()
    async with engine.connect() as raw:
        probe = await raw.execution_options(isolation_level="AUTOCOMMIT")
        started = loop.time()
        while True:
            # Checked BEFORE the probe: a contender that has already finished was never going to be
            # observed, and a stale "waiting" row from some other backend must not paper over that.
            if contender.done():
                raise LockBarrierError(
                    "LOCK BARRIER: the contender finished without ever being observed queued on a lock "
                    f"({_contender_outcome(contender)}). It never contended for the held lock -- for a "
                    "lock-serialization test that is the regression; otherwise the contender failed before "
                    "reaching the lock. This is the barrier, not the race assertion under test."
                )
            if (await probe.execute(text(BLOCKED_WAITER_SQL))).scalar():
                return
            if loop.time() - started > ceiling:
                raise LockBarrierError(
                    f"LOCK BARRIER: after {ceiling:.0f}s the contender is still running but was never observed "
                    "queued on a lock in this database -- it is wedged on something that is not the held lock. "
                    "This is the barrier's hang protection, not the race assertion under test."
                )
            await asyncio.sleep(POLL_INTERVAL_SEC)


async def run_contender_behind_held_lock[T](
    session_factory: async_sessionmaker[AsyncSession],
    holder: AsyncSession,
    contender: Coroutine[Any, Any, T],
    *,
    release: Callable[[], Awaitable[None]],
    ceiling: float = WEDGED_CONTENDER_CEILING_SEC,
) -> T:
    """Launch ``contender``, call ``release`` once it is observed queued behind ``holder``'s lock, return its result.

    ``holder`` must already hold the lock the contender will need. It is closed on every path -- on a
    barrier failure that rollback is what releases the lock, so the contender can finish and be awaited
    here rather than left pending (and its outcome is retrieved, so it never logs "exception was never
    retrieved" over the barrier's own, more useful, failure).
    """
    task = asyncio.create_task(contender)
    try:
        await wait_for_blocked_waiter(session_factory, task, ceiling=ceiling)
        await release()
    finally:
        await holder.close()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()
    return task.result()
