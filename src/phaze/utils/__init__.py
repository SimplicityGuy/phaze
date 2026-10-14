"""Pure-function utility helpers shared across phaze.

Phase 29 establishes this package for the relative-time helper consumed by the
admin/agents UI. Future phases may add humanize-style helpers here.
``raise_if_cancel_requested`` (phaze-gurn3) is the one asyncio helper: it lives here rather than
in its own module so the package inventory in ``docs/project-structure.md`` stays accurate.
"""

from __future__ import annotations

import asyncio


def raise_if_cancel_requested() -> None:
    """Raise ``CancelledError`` if the current task has an outstanding cancel request (phaze-gurn3).

    Call it where a long-lived loop is about to carry on after an iteration, so a cancellation
    converted into an exception (or swallowed outright) still ends the loop. Call it from a coroutine.

    A ``while True`` refresher that keeps going after ``except Exception`` relies on ``CancelledError``
    reaching it to stop. It does not always arrive. SQLAlchemy 2.1.4's pool ``__connect`` closes a
    connection whose connect event failed, catches ANY ``BaseException`` from that close, logs it as
    ``"Exception closing connection ... stranded by a failed connect event"``, and re-raises the
    connect event's ORIGINAL error. A cancel that lands during that close therefore comes out of the
    query as an ordinary ``Exception``; the loop logs it, sleeps, and runs again. Nothing cancels it a
    second time, so the shutdown awaiting it waits forever. Measured on the real test harness: the api
    lifespan hung in ``await review_counts_task`` while the orphan loop kept polling every 4 s, the same
    signature as the CI shard hangs.

    ``Task.cancelling()`` counts the cancel requests the task has not cleared with ``uncancel()``.
    ``asyncio.timeout`` and ``TaskGroup`` clear their own, and a library that swallows the error clears
    nothing, so a non-zero count means someone outside asked this task to stop.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
