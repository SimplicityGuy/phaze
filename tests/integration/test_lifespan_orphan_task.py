"""HYG-01 lifespan orphan-refresh task contract (Phase 91, Plan 01 -- Wave-0 scaffold).

Plan 91-02 wires a background orphan-count refresh loop into the FastAPI ``lifespan`` (mirroring the
``agent_worker`` heartbeat discipline: ``create_task`` on startup, cancel-first-then-await on shutdown,
before ``engine.dispose()``). This test drives the real app through its lifespan and pins two properties:

* **launch on startup** -- after the lifespan's startup phase, ``app.state.orphan_task`` exists and is an
  unfinished ``asyncio.Task`` (the refresh loop is live for the duration of the app).
* **clean cancel on shutdown** -- after the lifespan exits, the task is done + cancelled and awaiting it
  leaks no exception past the lifespan's own ``contextlib.suppress(asyncio.CancelledError)``.

``app.state.orphan_task`` is referenced only INSIDE the test body, so collection succeeds pre-91-02 and
the test runs RED (``AttributeError`` -- the attribute is absent until 91-02 launches the task). The app
is driven through ``app.router.lifespan_context`` (Starlette's own lifespan runner) in the current event
loop -- no second loop, no ``httpx`` client needed. This is the ``integration`` bucket: export BOTH
``TEST_DATABASE_URL`` and ``MIGRATIONS_TEST_DATABASE_URL`` on port 5433 (``just test-db``) -- a missing or
5432 URL looks like a colima flake but is a config gap, because the lifespan's startup ``SELECT 1`` needs a
reachable engine before the first refresh runs.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from phaze.main import create_app


pytestmark = pytest.mark.integration


async def test_lifespan_launches_and_cleanly_cancels_orphan_task() -> None:
    """The lifespan launches a background orphan-refresh task and cancels+awaits it cleanly on shutdown."""
    app = create_app()

    async with app.router.lifespan_context(app):
        # Startup complete: the refresh loop must be running (AttributeError here == clean RED pre-91-02).
        task = app.state.orphan_task
        assert isinstance(task, asyncio.Task)
        assert not task.done()  # the loop keeps running for the whole app lifetime

    # Shutdown complete: the lifespan must have cancelled the task (before engine.dispose()) ...
    task = app.state.orphan_task
    assert task.done()
    assert task.cancelled()  # cancelled cleanly, not crashed out of the loop

    # ... and re-awaiting it must not surface a leaked CancelledError.
    with contextlib.suppress(asyncio.CancelledError):
        await task
