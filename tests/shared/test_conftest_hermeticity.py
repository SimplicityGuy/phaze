"""Mutation-safe fixture-contract test for the CLEAN-02 ``create_savepoint`` conftest (92-03).

A GREEN suite alone does NOT prove hermeticity (``feedback_mutation_test_guard_tests``): the whole
point of this file is to prove the two load-bearing properties of the rewired ``tests/conftest.py``
fixtures directly, and to fail LOUDLY if either regresses.

Properties proven here
----------------------
1. **Commit visible to a sibling on the same connection** -- an in-test ``session.commit()`` is seen by
   the independent :func:`verify` read session, because both bind to the one per-test ``_db_connection``
   (the ``create_savepoint`` outer-transaction funnel, RESEARCH CRITICAL wiring corollary).
2. **Rollback isolation** -- nothing a test commits survives into the next test (the per-test outer
   transaction is rolled back at teardown), so no committed ``Agent`` row collides on ``pk_agents``. The
   two ``*_probe_agent_*`` tests below each assert-then-create the SAME agent id, so whichever runs
   SECOND proves the first test's commit was discarded -- order-independently.
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
  "assert absent at start" fails (or the re-committed fileserver seed collides on ``pk_agents``). The
  rollback-isolation property (assertion 2) is what breaks.
* **(b) Drop the fan-out routing.** In ``tests/conftest.py`` delete (or comment out) the
  ``monkeypatch.setattr("phaze.database.async_session", ...)`` line in ``_route_stats_fanout`` (leave
  ``session`` otherwise intact). Re-run this file: ``get_stage_progress`` now opens its own production
  pool connection, cannot see the uncommitted per-test rows, and
  :func:`test_production_fanout_sees_in_test_seeded_row` flips to reading ``analyze.done == 0`` and
  fails. The production-fan-out visibility property (assertion 3) is what breaks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.services.pipeline import get_stage_progress


if TYPE_CHECKING:
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
