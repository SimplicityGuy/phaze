"""Shared stubs for the controller-startup tests in this package (phaze-csh39).

``phaze.tasks.controller.startup`` opens a real session to run its two boot reconciles, and every
startup test in this package patches ``async_sessionmaker`` away so no Postgres connection is
opened. The patch each of them used was ``lambda *_a, **_kw: MagicMock()`` -- which is wrong in a
way that is invisible until you read the warnings summary.

``AsyncSession`` is not uniformly async. ``execute`` / ``commit`` are coroutines; ``add`` and
``begin_nested`` are ordinary synchronous calls that return an object (``begin_nested`` returns an
``AsyncSessionTransaction``, an async context manager). A blanket mock makes them ALL async, so
``async with session.begin_nested():`` -- the SAVEPOINT that isolates both reconcile reads
(``reenqueue.backfill_ledger_from_saq_jobs``, ``services.pipeline.jobs.count_inflight_jobs``) --
receives a coroutine instead of a context manager, raises ``TypeError``, and lands in the
``except Exception`` arm that exists for a pre-migration database. Both call sites then log
``*_degraded`` and return their zero tally, so seventeen tests across four files were asserting
against a degraded path while their names claimed the normal one. Nothing failed: the only symptom
was ``RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited``.

The general form, which is the part worth carrying: **a mock that gets a method's sync/async
nature wrong does not fail the test, it silently reroutes it.** Where the wrong shape raises into
a broad ``except``, the test keeps passing and starts measuring the fallback -- so the mock has to
model the sync/async split of the real object, not just its method names.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def make_stub_session_factory() -> MagicMock:
    """Return a stand-in for what ``async_sessionmaker(...)`` RETURNS (the factory, not the maker).

    Patch it in as ``lambda *_a, **_kw: make_stub_session_factory()`` so
    ``ctx["async_session"]()`` yields a session that models ``AsyncSession``'s sync/async split:

    - ``begin_nested()`` is SYNC and returns a real async context manager, so the SAVEPOINT arms
      and both reconcile reads take their normal branch rather than their degrade branch.
    - ``add()`` is SYNC.
    - ``execute()`` and ``scalars()`` are async and resolve to a SYNC result -- one whose
      ``.all()`` is empty and whose ``.scalar()`` is ``0``. Both halves matter:

      * The result must be a ``MagicMock``, not the ``AsyncMock`` child a bare ``AsyncMock``
        hands back. ``(await session.scalars(...)).all()`` on an async result returns a
        COROUTINE, and ``reenqueue._in_flight_cloud_job_ids`` then iterates it -- ``TypeError:
        'coroutine' object is not iterable``, swallowed by ``_run_boot_reconcile_with_retry``'s
        non-retryable arm. That is the same defect one layer deeper than ``begin_nested``, and
        it is why fixing only ``begin_nested`` moved the RuntimeWarning rather than removing it.
      * The values must be explicit. ``int(MagicMock())`` is ``1``, so an unconfigured
        ``.scalar()`` would turn ``count_inflight_jobs`` into a NON-ZERO in-flight count and
        quietly re-point the queue-loss detector these tests boot through.

      Empty results and a zero count are what the degraded path effectively produced, so no
      existing assertion's numbers move -- but ``recover_orphaned_work`` now runs to completion
      instead of aborting mid-way, which is the branch every one of these tests is named for.
    """
    result = MagicMock()
    result.all.return_value = []
    result.first.return_value = None
    result.one_or_none.return_value = None
    result.scalar.return_value = 0
    result.scalars.return_value.all.return_value = []
    result.mappings.return_value.all.return_value = []

    @asynccontextmanager
    async def _savepoint() -> AsyncIterator[None]:
        yield

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.scalars = AsyncMock(return_value=result)
    session.begin_nested = MagicMock(side_effect=_savepoint)
    session.add = MagicMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    factory.stub_session = session
    return factory
