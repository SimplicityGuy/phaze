"""phaze-gtd3: ``_LANE_MERGE_SQL`` must survive two genuinely concurrent lane beats (real PG).

The bug: an earlier version of ``_LANE_MERGE_SQL`` read its merge source from a
``FROM (SELECT ... FROM agents a WHERE a.id = :agent_id) AS merged`` self-join. Its docstring
claimed the single statement was atomic, but under READ COMMITTED that is not how Postgres
resolves a concurrent conflict: when beat B's UPDATE blocks on beat A's row lock and A commits
first, EvalPlanQual re-fetches only the TARGET row (``agents``) -- the FROM-clause self-join
(``merged.js``) keeps B's ORIGINAL pre-A-commit snapshot. B's write was therefore built from
stale data and clobbered the lane A had just committed.

This only reproduces across TWO INDEPENDENT, committed-visible connections with a genuine lock
wait -- the hermetic single-connection ``session`` fixture (savepoint-scoped) cannot express it,
so this lives on the real-PG ``committed_db`` fixture, following the established pattern in
``tests/integration/test_agent_push_concurrency.py`` / ``test_reconcile_concurrency.py``.

``test_concurrent_lane_beats_do_not_clobber_each_other`` is THE regression: session A merges the
``analyze`` lane but does not commit yet (holding the row lock); session B starts merging the
``fingerprint`` lane concurrently and must genuinely block on A's lock (asserted via a timeout
window) rather than racing ahead against a stale snapshot. Only after A commits does B proceed
via EvalPlanQual -- against the FIXED SQL, B's direct ``agents.last_status`` column references are
re-evaluated against the just-committed row, so BOTH lanes survive.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from phaze.models.agent import Agent
from phaze.routers.agent_heartbeat import _LANE_MERGE_SQL


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_AGENT_ID = "heartbeat-race"


async def _merge_lane(session: AsyncSession, lane: str, depth: int, *, pid: int = 1234) -> None:
    """Execute ``_LANE_MERGE_SQL`` for one lane beat -- mirrors the router's laned branch exactly."""
    payload = {"agent_version": "4.0.0", "worker_pid": pid, "queue_depth": depth}
    base = {k: v for k, v in payload.items() if k != "queue_depth"}
    await session.execute(
        _LANE_MERGE_SQL,
        {"agent_id": _AGENT_ID, "lane": lane, "base": json.dumps(base), "lane_status": json.dumps(payload)},
    )


async def test_concurrent_lane_beats_do_not_clobber_each_other(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Two overlapping lane beats, serialized by the row lock, must BOTH survive in last_status.

    Reproduces the exact failure the self-join version had: B's snapshot predates A's commit, so
    if the merge source came from anywhere other than the target row's own current column, B would
    overwrite A's committed lane. The fixed SQL closes that window.
    """
    _engine, session_factory = committed_db
    async with session_factory() as seed:
        seed.add(Agent(id=_AGENT_ID, name=_AGENT_ID, kind="fileserver", scan_roots=[]))
        await seed.commit()

    async with session_factory() as session_a, session_factory() as session_b:
        # A merges 'analyze' but does NOT commit yet -- this takes and holds the agents row lock,
        # and B's snapshot (taken once B's statement starts) predates A's eventual commit.
        await _merge_lane(session_a, "analyze", 100)

        task_b = asyncio.create_task(_merge_lane(session_b, "fingerprint", 200))
        await asyncio.sleep(0.3)
        assert not task_b.done(), (
            "B must genuinely block on A's held row lock -- if it finished immediately, A's lock "
            "isn't actually being held and this test is not exercising the EvalPlanQual race at all."
        )

        # Release A: it commits, dropping the row lock so B's blocked UPDATE can proceed. Under the
        # fixed SQL, B's EvalPlanQual re-check re-evaluates agents.last_status against the row A just
        # committed (a direct target-table column reference, not a FROM-clause snapshot).
        await session_a.commit()
        await asyncio.wait_for(task_b, timeout=10.0)
        await session_b.commit()

    async with session_factory() as session:
        agent = (await session.execute(select(Agent).where(Agent.id == _AGENT_ID))).scalar_one()
        assert agent.last_status is not None
        assert set(agent.last_status["lanes"]) == {"analyze", "fingerprint"}, (
            "both lanes must survive the race -- a lost lane here is exactly the phaze-gtd3 clobber"
        )
        assert agent.last_status["lanes"]["analyze"]["queue_depth"] == 100
        assert agent.last_status["lanes"]["fingerprint"]["queue_depth"] == 200
        assert agent.last_status["queue_depth"] == 300, "the cross-lane SUM must reflect both committed lanes"


async def test_sequential_lane_beats_both_survive(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Baseline (non-racing) sanity check: two lane beats run one after another, in order, both persist.

    Cheap functional coverage independent of timing, alongside the genuine race test above.
    """
    _engine, session_factory = committed_db
    async with session_factory() as seed:
        seed.add(Agent(id=_AGENT_ID, name=_AGENT_ID, kind="fileserver", scan_roots=[]))
        await seed.commit()

    async with session_factory() as session:
        await _merge_lane(session, "meta", 5)
        await session.commit()
    async with session_factory() as session:
        await _merge_lane(session, "io", 7)
        await session.commit()

    async with session_factory() as session:
        agent = (await session.execute(select(Agent).where(Agent.id == _AGENT_ID))).scalar_one()
        assert agent.last_status is not None
        assert set(agent.last_status["lanes"]) == {"meta", "io"}
        assert agent.last_status["queue_depth"] == 12
