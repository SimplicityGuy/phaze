"""Mutation-safe fixture-contract test for the CLEAN-02 ``create_savepoint`` conftest (92-03).

A GREEN suite alone does NOT prove hermeticity (``feedback_mutation_test_guard_tests``): the whole
point of this file is to prove the two load-bearing properties of the rewired ``tests/conftest.py``
fixtures directly, and to fail LOUDLY if either regresses.

Properties proven here
----------------------
1. **Commit visible to a sibling on the same connection** -- an in-test ``session.commit()`` is seen by
   the independent :func:`verify` read session, because both bind to the one per-test ``_db_connection``
   (the ``create_savepoint`` outer-transaction funnel, RESEARCH CRITICAL wiring corollary).
2. **Rollback isolation, WITHIN one test's use of** :func:`verify` **-- NOT a check of the outer
   transaction itself (see phaze-8mwhk).** The two ``*_probe_agent_*`` tests below each assert-then-
   create the SAME agent id via :func:`verify`. That looked like an across-test rollback check, but
   :func:`verify`'s OWN inner savepoint is opened by its leading "assert absent" read, which runs BEFORE
   the write -- so the write's later release lands inside :func:`verify`'s still-open savepoint rather
   than the true outer transaction, and :func:`verify`'s own (uncommitted) close discards it at teardown
   regardless of what the outer transaction does. **These two tests pass identically whether the
   session fixture's outer rollback is intact or is mutated into a commit** -- see Property 5 below for
   the probe pair that actually discriminates that mutation.
3. **Production-fan-out visibility (guards the BLOCKER-1 regression)** -- a seed-then-read against the
   REAL ``get_stage_progress`` fan-out sees the per-test savepoint state (non-zero), because 92-03
   Task 2 routes ``phaze.database.async_session`` onto the same per-test connection + serializes the
   fan-out with ``Semaphore(1)``. Without that routing the fan-out opens its own pool connection, reads
   under read-committed isolation, and degrades to ZERO.

Mutation recipes (run these to confirm the test has teeth -- do NOT leave a broken variant committed)
----------------------------------------------------------------------------------------------------
* **(a) Revert the hermetic engine.** In ``tests/conftest.py`` change ``async_engine`` back to a
  function-scoped fixture that does ``create_all``/``drop_all`` per test AND commits a per-test
  ``test-fileserver`` seed (the pre-92-03 shape), and change ``session`` back to a plain
  ``async_sessionmaker(async_engine)`` session with no outer-transaction rollback. Re-run this file:
  the surviving committed ``Agent`` row now persists across tests, so the SECOND-running probe test's
  "assert absent at start" fails (or the re-committed fileserver seed collides on ``pk_agents``).
* **(b) Drop the fan-out routing.** In ``tests/conftest.py`` delete (or comment out) the
  ``monkeypatch.setattr("phaze.database.async_session", ...)`` line in ``_route_stats_fanout`` (leave
  ``session`` otherwise intact). Re-run this file: ``get_stage_progress`` now opens its own production
  pool connection, cannot see the uncommitted per-test rows, and
  :func:`test_production_fanout_sees_in_test_seeded_row` flips to reading ``analyze.done == 0`` and
  fails. The production-fan-out visibility property (assertion 3) is what breaks.
* **(c) Mutate the outer rollback into a commit (phaze-8mwhk).** In ``tests/conftest.py``'s ``session``
  fixture, change ``await _finalize_shared_connection_session(s, leaked, violations, rollback=outer.rollback)``
  to pass ``rollback=outer.commit`` instead. Re-run this file: the two Property-5 tests below
  (``test_outer_rollback_discriminates_leak_probe_a`` / ``_b``) fail with "LEAK: row from another test"
  on whichever runs SECOND, while ``test_probe_agent_commit_visible_then_rolled_back_a`` / ``_b`` (the
  Property-2 tests above) stay GREEN -- proving those do not catch this regression. Revert before
  committing; the module-scoped :func:`_cleanup_leak_probe_agent_at_module_end` deletes the leaked row
  either way, once this module's tests are done.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.services.pipeline import get_stage_progress
from tests.conftest import TEST_DATABASE_URL, _finalize_shared_connection_session


if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.ext.asyncio import AsyncSession

# A stable id reused by BOTH probe tests so the second-running one proves the first's commit rolled back.
_PROBE_AGENT_ID = "hermetic-probe-agent"


async def _agent_present(read_session: AsyncSession, agent_id: str) -> bool:
    """True iff an ``Agent`` with ``agent_id`` is visible to ``read_session`` (fresh SELECT, no cache)."""
    row = (await read_session.execute(select(Agent.id).where(Agent.id == agent_id))).scalar_one_or_none()
    return row is not None


async def test_probe_agent_commit_visible_then_rolled_back_a(session: AsyncSession, verify: AsyncSession) -> None:
    """Commit an Agent via ``session``; the sibling ``verify`` read sees it -- and it must start ABSENT.

    ABSENT-at-start proves the OTHER probe test's identical commit was rolled back at its teardown
    (order-independent). VISIBLE-after-commit proves the in-test commit reaches a sibling on the same
    per-test connection.
    """
    assert not await _agent_present(verify, _PROBE_AGENT_ID), "row leaked from another test -- rollback isolation is broken"

    session.add(Agent(id=_PROBE_AGENT_ID, name=_PROBE_AGENT_ID, kind="fileserver", scan_roots=[]))
    await session.commit()

    assert await _agent_present(verify, _PROBE_AGENT_ID), "sibling verify session did not see the in-test commit"


async def test_probe_agent_commit_visible_then_rolled_back_b(session: AsyncSession, verify: AsyncSession) -> None:
    """Identical to variant ``_a`` with the same agent id -- whichever runs second proves the rollback."""
    assert not await _agent_present(verify, _PROBE_AGENT_ID), "row leaked from another test -- rollback isolation is broken"

    session.add(Agent(id=_PROBE_AGENT_ID, name=_PROBE_AGENT_ID, kind="fileserver", scan_roots=[]))
    await session.commit()

    assert await _agent_present(verify, _PROBE_AGENT_ID), "sibling verify session did not see the in-test commit"


async def test_production_fanout_sees_in_test_seeded_row(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Seed a file + completed analysis, then the REAL get_stage_progress fan-out must read it (non-zero).

    This is the mutation-safe guard for BLOCKER-1: ``get_stage_progress`` opens its OWN sessions from
    ``phaze.database.async_session``; 92-03 Task 2 routes those onto the per-test connection. If that
    routing regresses, the fan-out reads a different (empty) transaction and ``analyze.done`` degrades
    to 0 -- so asserting ``== 1`` here fails loudly.
    """
    file = await make_file(original_filename="hermetic-analyzed.mp3")
    # done(analyze) requires analysis_completed_at NOT NULL (DERIV-03 / Phase 82 cutover).
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=file.id, bpm=128.0, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    progress = await get_stage_progress(session)

    assert progress["analyze"]["done"] == 1, "routed production fan-out did not see the per-test seeded row (reads zero?)"
    assert progress["analyze"]["total"] == 1, "music/video denominator did not reflect the seeded file"
    assert progress["metadata"]["done"] == 0, "no metadata row was seeded, yet metadata.done is non-zero"


# Property 5 (phaze-8mwhk): a probe pair that actually discriminates a regression of the outer rollback
# into a commit -- the ``*_probe_agent_*`` tests above (Property 2) cannot, for the reason spelled out in
# the module docstring: their write is released into `verify`'s already-open savepoint, not the true
# outer transaction, so `verify`'s own uncommitted close discards it before the outer transaction's fate
# is ever consulted. Found by dev/conns while implementing phaze-1dc8g; a throwaway probe pair written
# there caught it with the same shape used here.
#
# THE FIX: write through `session` ALONE. No other session queries the shared connection first in these
# two tests, so `session.commit()` releases its savepoint directly into `outer` (there is nothing else on
# the stack for it to land inside instead). A session-level Postgres ``SET`` is committed alongside the
# row for the same reason dev/conns' probe carried one: both are TRANSACTIONAL in Postgres (a `SET` made
# inside an aborted transaction is undone by the abort, same as a row) and both are exercised through the
# identical write path, so if a future change ever made row-level and GUC-level state diverge here that
# divergence would show up as one assertion failing without the other. Under the CURRENT per-test
# `NullPool` connection (a fresh physical connection every test -- see `async_engine`), the GUC half can
# never itself observe a cross-test leak, because a session-level `SET` cannot survive past the physical
# connection it was set on regardless of commit or rollback; it is kept as a documented, always-green
# companion to the row check, not a claim that it independently discriminates this mutation.
#
# ORDER-INDEPENDENCE: both tests share the same stable id and do "assert absent, then write", exactly
# like the Property-2 pair -- whichever of the two runs SECOND is the one that actually proves the FIRST
# one's write did not survive.
_LEAK_AGENT_ID = "hermetic-leak-probe-agent"
_LEAK_SESSION_GUC = "phaze.hermetic_leak_probe"


async def _leak_guc_value(target: AsyncSession) -> str | None:
    """Current value of a custom, never-pre-registered session GUC -- empty/NULL if never ``SET``."""
    return (await target.execute(text(f"SELECT current_setting('{_LEAK_SESSION_GUC}', true)"))).scalar_one()


async def _assert_no_leak_from_another_test(target: AsyncSession) -> None:
    assert not await _agent_present(target, _LEAK_AGENT_ID), "LEAK: row from another test survived the per-test outer transaction"
    guc = await _leak_guc_value(target)
    assert not guc, f"LEAK: session-level SET from another test survived the per-test outer transaction (got {guc!r})"


async def _seed_leak_through_session_alone(target: AsyncSession) -> None:
    """Write a row AND a session-level ``SET`` through ``target`` only, then commit.

    No sibling session (e.g. ``verify``) is queried first, so this commit has nothing else on the shared
    connection's savepoint stack to land inside -- it releases straight into ``outer``, which is exactly
    what makes the mutation in recipe (c) above observable.
    """
    target.add(Agent(id=_LEAK_AGENT_ID, name=_LEAK_AGENT_ID, kind="fileserver", scan_roots=[]))
    await target.execute(text(f"SET {_LEAK_SESSION_GUC} = 'leaked-from-probe'"))
    await target.commit()


async def _delete_leak_probe_agent() -> None:
    """Remove ``_LEAK_AGENT_ID`` on a throwaway engine/connection, independent of any fixture or loop.

    A fresh :func:`create_async_engine` (not the ``async_engine`` fixture) so this has no dependency on
    any per-test or session-scoped fixture still being alive, and can run from inside a bare
    ``asyncio.run`` after every per-test event loop in this module has already closed.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DELETE FROM agents WHERE id = :agent_id"), {"agent_id": _LEAK_AGENT_ID})
            await conn.commit()
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_leak_probe_agent_at_module_end() -> Generator[None]:
    """Guarantee ``_LEAK_AGENT_ID`` never survives THIS MODULE's run, however it got there.

    Deliberately MODULE-scoped, not per-test: a per-test cleanup would erase test A's leaked row before
    test B ever got to look for it, defeating the whole point of Property 5. Scoping it to the module
    instead means cleanup runs exactly once, after both probe tests (in whichever order) have already had
    their ``session`` fixture's outer transaction resolved -- so a genuine leak from recipe (c) is still
    fully observable within this run, while nothing survives into a later, unrelated test module or a
    later full-suite run. Runs via ``asyncio.run`` in a private event loop rather than any fixture's own,
    since no per-test loop is guaranteed to still be alive at module-teardown time.
    """
    yield
    asyncio.run(_delete_leak_probe_agent())


async def test_outer_rollback_discriminates_leak_probe_a(session: AsyncSession) -> None:
    """Discriminates the phaze-8mwhk mutation (recipe (c)); the Property-2 probes above cannot."""
    await _assert_no_leak_from_another_test(session)
    await _seed_leak_through_session_alone(session)


async def test_outer_rollback_discriminates_leak_probe_b(session: AsyncSession) -> None:
    """Identical to variant ``_a`` -- whichever of the two runs SECOND proves the outer rollback ran."""
    await _assert_no_leak_from_another_test(session)
    await _seed_leak_through_session_alone(session)


# Property 4 (phaze-5lq8a): the teardown ORDERING that keeps property 2 true when the close FAILS.
#
# `session`'s finalizer used to be two sequential awaits -- `await s.close()` then
# `await outer.rollback()`. `s.close()` is precisely what raises when another session on the shared
# connection released a savepoint out of order (`InvalidSavepointSpecificationError`), and a raising
# close skipped the rollback entirely: the per-test outer transaction was left OPEN on a connection
# about to be handed back, i.e. rollback isolation silently stopped holding in exactly the situation
# that most needs it. `_finalize_shared_connection_session` puts the rollback in a `finally`.
#
# Driven against doubles rather than a live fixture because the failure needs `close()` to RAISE, and
# there is no way to make the real session do that on demand without also destroying the connection
# the assertion would have to read back through.
class _ExplodingSession:
    """Stands in for the `AsyncSession` whose `close()` hits an already-discarded savepoint."""

    def __init__(self, error: Exception | None) -> None:
        self._error = error
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self._error is not None:
            raise self._error


class _RecordingRollback:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


async def test_the_outer_rollback_runs_even_when_the_close_raises() -> None:
    """The regression this pins: a raising `close()` must not be able to skip rollback isolation."""
    boom = RuntimeError('savepoint "sa_savepoint_7" does not exist')
    s = _ExplodingSession(boom)
    rollback = _RecordingRollback()

    with pytest.raises(RuntimeError) as exc_info:
        await _finalize_shared_connection_session(s, leaked=[], violations=[], rollback=rollback)  # type: ignore[arg-type]

    assert exc_info.value is boom, "the close's own failure must still reach the report, not be swallowed"
    assert rollback.calls == 1, "the outer transaction was left OPEN when close() raised -- hermeticity lost (phaze-5lq8a)"


async def test_the_outer_rollback_runs_exactly_once_on_the_clean_path() -> None:
    """And the fix must not double-roll-back, or rollback the wrong number of times, when nothing fails."""
    s = _ExplodingSession(None)
    rollback = _RecordingRollback()

    await _finalize_shared_connection_session(s, leaked=[], violations=[], rollback=rollback)  # type: ignore[arg-type]

    assert s.closed and rollback.calls == 1


async def test_a_savepoint_violation_is_reported_without_discarding_the_close_failure() -> None:
    """The diagnosis replaces the HEADLINE, never the evidence: asyncpg's error stays as __context__.

    A finalizer that hid a genuinely broken session state would trade a visible failure for an
    invisible one -- the thing phaze-5lq8a exists to stop, not to reintroduce one layer up.
    """
    boom = RuntimeError('savepoint "sa_savepoint_7" does not exist')
    s = _ExplodingSession(boom)
    rollback = _RecordingRollback()

    with pytest.raises(AssertionError) as exc_info:
        await _finalize_shared_connection_session(
            s,
            leaked=[],
            violations=["sa_savepoint_5 was released with sa_savepoint_6 still established above it"],
            rollback=rollback,
        )  # type: ignore[arg-type]

    assert "released OUT OF ORDER" in str(exc_info.value)
    assert exc_info.value.__context__ is boom, "the underlying asyncpg failure was discarded instead of chained"
    assert rollback.calls == 1, "the rollback must still run on the violation path"


async def test_the_read_session_finalizer_takes_no_rollback() -> None:
    """`verify` owns no outer transaction, so it must close without attempting one."""
    s = _ExplodingSession(None)

    await _finalize_shared_connection_session(s, leaked=[], violations=[])  # type: ignore[arg-type]

    assert s.closed
