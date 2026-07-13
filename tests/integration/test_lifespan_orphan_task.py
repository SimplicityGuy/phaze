"""HYG-01 lifespan orphan-refresh task contract (Phase 91).

Plan 91-02 wires a background orphan-count refresh loop into the FastAPI ``lifespan`` (mirroring the
``agent_worker`` heartbeat discipline: ``create_task`` on startup, cancel-first-then-await on shutdown,
before ``engine.dispose()``). This test drives the real app through its lifespan and pins two properties:

* **launch on startup** -- after the lifespan's startup phase, ``app.state.orphan_task`` exists and is an
  unfinished ``asyncio.Task`` (the refresh loop is live for the duration of the app).
* **clean cancel on shutdown** -- after the lifespan exits, the task is done + cancelled and awaiting it
  leaks no exception past the lifespan's own ``contextlib.suppress(asyncio.CancelledError)``.

The app is driven through ``app.router.lifespan_context`` (Starlette's own lifespan runner) in the current
event loop -- no second loop, no ``httpx`` client needed. The lifespan's *incidental* startup collaborators
(the ``alembic upgrade`` auto-migration, the ``/saq`` dashboard mount, and the ``postgres:5432/phaze``
default broker) are orthogonal to the orphan-task wiring under test, so they are neutralised via the
established ``settings`` monkeypatch idiom rather than relying on ambient env:

* ``auto_migrate=False`` -- the integration harness provisions the schema via ``Base.metadata.create_all``
  (no alembic stamp), so a lifespan ``alembic upgrade head`` would ``DuplicateTableError`` on ``files``.
* ``enable_saq_ui=False`` -- skip the ``/saq`` mount (it queries the agents table + opens per-agent pools).
* ``queue_url`` / ``redis_url`` -- point the controller queue at the reachable integration broker (the
  same libpq DSN derivation the other live-broker integration tests use) instead of the unreachable
  ``postgres:5432/phaze`` default, so ``controller_queue.connect()`` (and the reverse-order shutdown that
  dereferences it) succeed. This is the ``integration`` bucket: it needs ``TEST_DATABASE_URL`` (port 5433
  locally via ``just test-db``; 5432 in CI) so the module engine's startup ``SELECT 1`` has a reachable DB.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest

from phaze.config import settings
from phaze.main import create_app


pytestmark = pytest.mark.integration

# Raw libpq broker DSN (NOT the ``+asyncpg`` dialect psycopg3 cannot parse), derived the same way the
# other live-broker integration tests derive it: prefer PHAZE_QUEUE_URL, else TEST_DATABASE_URL with the
# SQLAlchemy dialect suffix stripped. Points the controller queue at the reachable integration broker DB.
_BROKER_DSN = (os.environ.get("PHAZE_QUEUE_URL") or os.environ.get("TEST_DATABASE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")).replace(
    "postgresql+asyncpg://", "postgresql://"
)
_CACHE_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")


async def test_lifespan_launches_and_cleanly_cancels_orphan_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan launches a background orphan-refresh task and cancels+awaits it cleanly on shutdown."""
    # Neutralise the incidental startup collaborators (see module docstring) so the test exercises the
    # orphan-task wiring, not the alembic/SAQ-UI/broker-default machinery.
    monkeypatch.setattr(settings, "auto_migrate", False)
    monkeypatch.setattr(settings, "enable_saq_ui", False)
    monkeypatch.setattr(settings, "queue_url", _BROKER_DSN)
    monkeypatch.setattr(settings, "redis_url", _CACHE_REDIS_URL)

    app = create_app()

    async with app.router.lifespan_context(app):
        # Startup complete: the refresh loop must be running.
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
