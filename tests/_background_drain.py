"""The single strict drain for the pipeline router's detached background tasks (phaze-5lq8a).

WHY A LEAKED BACKGROUND TASK CORRUPTS AN UNRELATED FIXTURE
----------------------------------------------------------

Every session in a DB test binds to ONE per-test connection (``tests/conftest.py::_db_connection``)
with ``join_transaction_mode="create_savepoint"`` -- the test's :func:`session`, the app's
``get_session`` override, the :func:`verify` read session, AND, because
``tests/conftest.py::_route_stats_fanout`` monkeypatches ``phaze.database.async_session`` onto that
same connection, every session the PRODUCTION code opens for itself.

Postgres savepoints on a connection are a LIFO STACK, and ``RELEASE SAVEPOINT s`` destroys ``s``
*and every savepoint established after it*. So that whole arrangement is sound only while the
sessions sharing the connection nest strictly LIFO. A router background task
(``asyncio.create_task`` + the ``_background_tasks`` discipline) breaks LIFO the moment it outlives
the test body, because it opens and releases its own savepoint on a timeline nothing coordinates:

1. the background task's session opens savepoint N;
2. the test body runs on -- ``verify``'s first ``execute()`` opens savepoint N+1;
3. the background task commits: ``RELEASE SAVEPOINT N`` destroys N+1 as collateral;
4. teardown: ``verify.close()`` issues ``ROLLBACK TO SAVEPOINT N+1`` ->
   ``asyncpg.exceptions.InvalidSavepointSpecificationError: savepoint "sa_savepoint_N+1" does not
   exist``, preceded by ``SAWarning: nested transaction already deassociated from connection``.

The test body has already passed by then, so the run reports ``1 passed ... 1 error`` and pytest
exits 1 -- a whole-suite exit code destroyed by a test that succeeded.

WHY IT LOOKED LIKE "ONLY ON A DATABASE THAT HAS ALREADY HOSTED A SESSION"
------------------------------------------------------------------------

It is not the database. Steps 1-3 are a race between the background task and the test body's next
``await``, so what decides the outcome is how much else is competing for the event loop and the
connection -- i.e. how many other tests are running, and how loaded the machine is. Run the one file
on a fresh seat and the enqueue loop finishes inside the POST's own await points, the ordering stays
LIFO, and the defect is invisible. That is why it was reported as fresh-DB-green / reused-DB-red and
why the correction on phaze-5lq8a could not reproduce it from the same rule.

THE RULE
--------

A test that triggers a backgrounding endpoint MUST call :func:`drain_router_background_tasks` before
it asserts, and this drain must either finish or RAISE -- never return with work still in flight.
Its predecessor (a bounded ``for _ in range(500): await asyncio.sleep(0)`` spin, copied into six
modules) failed both halves: ``sleep(0)`` only yields to the event loop and never waits for I/O, and
when the 500 yields ran out it returned SILENTLY, handing the caller exactly the corrupted savepoint
stack above with no indication anything had gone wrong. ``tests/conftest.py`` backstops this: the
:func:`session` and :func:`verify` finalizers fail the test outright when it leaks a task, so a
missing drain surfaces as a named diagnosis instead of an asyncpg traceback from a fixture the test
never mentions.

AND NO, THIS CANNOT BE MOVED INTO A FIXTURE
-------------------------------------------

The obvious "simplification" is to delete the per-test calls and drain once in a fixture instead.
It does not work, and the reason is teardown ORDER rather than taste.

pytest finalizes in reverse setup order, so a fixture always tears down BEFORE the fixtures it
depends on. ``verify`` depends on ``session``, which means ``verify`` unwinds FIRST -- and ``verify``
is the session whose savepoint the leaked task destroys. Any fixture that could host a drain is
therefore either ``verify`` itself or something ``verify`` is built on, i.e. something that runs
AFTER the damage. Worse, draining at that point cannot undo it: by then the background task has
already issued its ``RELEASE``, and a released savepoint does not come back.

So the split is absolute, and it is the reason the call sites are not redundant with the guard:

* only a drain IN THE TEST BODY, before the first read through ``verify``, PREVENTS the corruption;
* the ``conftest`` guard can only DIAGNOSE it afterwards.

Deleting a ``drain_router_background_tasks()`` call because "the fixtures catch it anyway" trades
prevention for a post-mortem. The guard going quiet afterwards is not evidence the drain was
unnecessary; on a fast machine the race simply does not fire (measured: with three such calls
removed, a 7050-test suite still passed with zero guard hits).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Sequence


# Generous: a drained task does two or three database round-trips, so this is not a latency budget --
# it is the point at which "still running" stops being slow and starts being wedged. It is a TOTAL
# deadline, not a per-iteration one; the predecessor's per-iteration timeout multiplied by its
# iteration cap gave a nominal ceiling of 5000 s that nothing would ever wait out.
DRAIN_TIMEOUT_SEC = 30.0


def pending_router_background_tasks() -> list[asyncio.Task[Any]]:
    """Return the router's unfinished background tasks that belong to the RUNNING event loop.

    Tasks bound to another loop are excluded deliberately. pytest-asyncio gives each test its own
    loop, so a foreign task is one an EARLIER test leaked and abandoned; its loop is closed, so it
    can never complete, and awaiting it here would block until the deadline on every subsequent
    call. It also cannot touch this test's connection, so it is not this test's problem -- the
    ``tests/conftest.py`` guard fails the test that actually leaked it, at the point it leaked.
    """
    import phaze.routers.pipeline as pipeline_mod

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return []
    return [task for task in set(pipeline_mod._background_tasks) if not task.done() and task.get_loop() is loop]


def leaked_background_tasks_message(tasks: Sequence[asyncio.Task[Any]]) -> str:
    """Explain, at the point of failure, what an undrained task did to the savepoint stack."""
    names = ", ".join(sorted(task.get_name() for task in tasks))
    return (
        f"this test left {len(tasks)} pipeline-router background task(s) running past its body: {names}. "
        "They open sessions on the SAME per-test connection this fixture uses, so their commits RELEASE "
        "savepoints out of order and destroy this session's -- surfacing as "
        "'InvalidSavepointSpecificationError: savepoint \"sa_savepoint_N\" does not exist' at teardown of a "
        "test that passed (phaze-5lq8a). Await tests._background_drain.drain_router_background_tasks() "
        "after the request that spawned them and before the first assertion."
    )


async def drain_router_background_tasks(timeout: float = DRAIN_TIMEOUT_SEC) -> None:
    """Await every pipeline-router background task to completion, or raise (phaze-5lq8a).

    The bulk pipeline triggers (``retry_analysis_failed``, ``trigger_metadata_extraction``,
    ``_route_discovered_by_duration``, the proposal/recovery/tracklist producers) detach their
    enqueue loop with ``asyncio.create_task`` so the HTTP response returns immediately, which means
    the response arriving proves nothing about the work having happened. Call this between the
    request and the assertions.

    Loops rather than awaiting one snapshot because a draining task may spawn another, and raises on
    the deadline rather than returning, because returning early is precisely the failure the module
    docstring describes: the caller then asserts against half-finished work and, worse, hands its
    fixtures a savepoint stack another session is still mutating.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = pending_router_background_tasks()
        if not pending:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(
                f"pipeline-router background tasks did not drain within {timeout:g}s: "
                f"{', '.join(sorted(task.get_name() for task in pending))}. "
                "Returning anyway would corrupt this test's savepoint stack -- see tests/_background_drain (phaze-5lq8a)."
            )
        await asyncio.wait(pending, timeout=remaining)
