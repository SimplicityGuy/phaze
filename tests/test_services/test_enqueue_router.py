"""Unit tests for src/phaze/services/enqueue_router.py (Phase 30 Plan 01).

The shared enqueue-routing foundation that every misrouted control-plane enqueue
(Plans 02-04) will call. Two exports under test:

- ``select_active_agent(session)`` — SELECT the most-recently-seen non-revoked
  agent (``revoked_at IS NULL`` AND ``last_seen_at IS NOT NULL``, ORDER BY
  ``last_seen_at DESC`` LIMIT 1). Raises ``NoActiveAgentError`` when none exist.
  The ``revoked_at IS NULL`` predicate excludes the permanently-revoked
  ``legacy-application-server`` seeded by the conftest.
- ``resolve_queue_for_task(task_name, app_state, session)`` — maps a task name to
  the queue an actual worker consumes: the named ``controller`` queue (agent_id
  None) for controller tasks, a ``phaze-agent-<id>`` queue (+ selected agent_id)
  for per-agent tasks, and a hard ``ValueError`` for any unknown task (fail loud,
  never silently hit the consumer-less default queue — the v4.0.6 incident).

Tests use the real PostgreSQL ``session`` fixture from ``tests/conftest.py``.
The conftest pre-seeds the LEGACY agent (``last_seen_at IS NULL``, so it is
naturally excluded by the active-agent filter), so test agents use distinct
kebab-case slugs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from phaze.models.agent import Agent
from phaze.services.enqueue_router import (
    AGENT_TASKS,
    CONTROLLER_TASKS,
    NoActiveAgentError,
    RoutedQueue,
    resolve_queue_for_task,
    select_active_agent,
)
from tests._queue_fakes import seed_active_agent, stub_app_state


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_agent(
    session: AsyncSession,
    *,
    agent_id: str,
    last_seen_at: datetime | None,
    revoked: bool = False,
) -> Agent:
    """Insert a kebab-case test agent with explicit liveness columns."""
    agent = Agent(
        id=agent_id,
        name=agent_id,
        token_hash=None,
        scan_roots=[],
        last_seen_at=last_seen_at,
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


# ---------------------------------------------------------------------------
# Task-set wiring
# ---------------------------------------------------------------------------


def test_task_sets_are_disjoint_frozensets() -> None:
    """The two task sets are frozensets and never overlap (a task routes one way)."""
    assert isinstance(CONTROLLER_TASKS, frozenset)
    assert isinstance(AGENT_TASKS, frozenset)
    assert CONTROLLER_TASKS.isdisjoint(AGENT_TASKS)
    # Spot-check the registered functions from controller.py / agent_worker.py.
    assert "generate_proposals" in CONTROLLER_TASKS
    assert "refresh_tracklists" in CONTROLLER_TASKS
    assert "process_file" in AGENT_TASKS
    assert "scan_directory" in AGENT_TASKS


# ---------------------------------------------------------------------------
# select_active_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_active_agent_returns_recent_agent(session: AsyncSession) -> None:
    """One non-revoked agent with a recent last_seen_at -> returned."""
    seeded = await _seed_agent(session, agent_id="fileserver-01", last_seen_at=datetime.now(UTC))

    agent = await select_active_agent(session)

    assert agent.id == seeded.id


@pytest.mark.asyncio
async def test_select_active_agent_prefers_most_recently_seen(session: AsyncSession) -> None:
    """Two active agents -> the one with the greater last_seen_at wins (deterministic)."""
    now = datetime.now(UTC)
    await _seed_agent(session, agent_id="fileserver-old", last_seen_at=now - timedelta(hours=1))
    newer = await _seed_agent(session, agent_id="fileserver-new", last_seen_at=now)

    agent = await select_active_agent(session)

    assert agent.id == newer.id


@pytest.mark.asyncio
async def test_select_active_agent_raises_when_only_revoked(session: AsyncSession) -> None:
    """A revoked agent (even with a recent last_seen_at) is excluded -> raises."""
    await _seed_agent(session, agent_id="fileserver-dead", last_seen_at=datetime.now(UTC), revoked=True)

    with pytest.raises(NoActiveAgentError):
        await select_active_agent(session)


@pytest.mark.asyncio
async def test_select_active_agent_raises_when_no_eligible_rows(session: AsyncSession) -> None:
    """Only the conftest LEGACY agent (last_seen_at IS NULL) is present -> raises."""
    with pytest.raises(NoActiveAgentError):
        await select_active_agent(session)


@pytest.mark.asyncio
async def test_select_active_agent_excludes_never_seen(session: AsyncSession) -> None:
    """A non-revoked agent that has never checked in (last_seen_at NULL) is excluded."""
    await _seed_agent(session, agent_id="fileserver-pending", last_seen_at=None)

    with pytest.raises(NoActiveAgentError):
        await select_active_agent(session)


# ---------------------------------------------------------------------------
# select_active_agent — kind scoping (Phase 49 D-13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_active_agent_kind_compute_returns_only_compute(session: AsyncSession) -> None:
    """kind='compute' returns the compute agent even when a newer fileserver exists."""
    await seed_active_agent(session, "fileserver-01", kind="fileserver")
    compute = await seed_active_agent(session, "compute-01", kind="compute")

    agent = await select_active_agent(session, kind="compute")

    assert agent.id == compute.id
    assert agent.kind == "compute"


@pytest.mark.asyncio
async def test_select_active_agent_kind_fileserver_excludes_compute(session: AsyncSession) -> None:
    """kind='fileserver' excludes compute agents, even a more-recently-seen one."""
    fileserver = await seed_active_agent(session, "fileserver-01", kind="fileserver")
    # Seeded second -> greater last_seen_at; would win without the kind filter.
    await seed_active_agent(session, "compute-01", kind="compute")

    agent = await select_active_agent(session, kind="fileserver")

    assert agent.id == fileserver.id
    assert agent.kind == "fileserver"


@pytest.mark.asyncio
async def test_select_active_agent_no_kind_preserves_back_compat(session: AsyncSession) -> None:
    """No kind -> most-recently-seen of ANY kind (existing callers unchanged)."""
    await seed_active_agent(session, "fileserver-01", kind="fileserver")
    compute = await seed_active_agent(session, "compute-01", kind="compute")

    agent = await select_active_agent(session)

    # compute-01 was seeded last (greater last_seen_at) -> wins regardless of kind.
    assert agent.id == compute.id


@pytest.mark.asyncio
async def test_select_active_agent_kind_absent_raises(session: AsyncSession) -> None:
    """kind='compute' with no compute agent online raises NoActiveAgentError."""
    await seed_active_agent(session, "fileserver-01", kind="fileserver")

    with pytest.raises(NoActiveAgentError):
        await select_active_agent(session, kind="compute")


# ---------------------------------------------------------------------------
# resolve_queue_for_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_controller_task_returns_controller_queue() -> None:
    """A controller task resolves to app_state.controller_queue with agent_id None."""
    app_state = stub_app_state()

    routed = await resolve_queue_for_task("generate_proposals", app_state, None)

    assert isinstance(routed, RoutedQueue)
    assert routed.queue is app_state.controller_queue
    assert routed.agent_id is None


@pytest.mark.asyncio
async def test_resolve_agent_task_returns_per_agent_queue(session: AsyncSession) -> None:
    """A per-agent task resolves to the selected agent's queue + its agent_id."""
    await _seed_agent(session, agent_id="fileserver-01", last_seen_at=datetime.now(UTC))
    app_state = stub_app_state()

    routed = await resolve_queue_for_task("process_file", app_state, session)

    assert routed.agent_id == "fileserver-01"
    assert routed.queue.name == "phaze-agent-fileserver-01"


@pytest.mark.asyncio
async def test_resolve_agent_task_without_session_raises() -> None:
    """A per-agent task requires a session to select the target agent."""
    app_state = stub_app_state()

    with pytest.raises(ValueError, match="session"):
        await resolve_queue_for_task("process_file", app_state, None)


@pytest.mark.asyncio
async def test_resolve_agent_task_no_active_agent_propagates(session: AsyncSession) -> None:
    """No eligible agent -> NoActiveAgentError surfaces (never silent default)."""
    app_state = stub_app_state()

    # Only the conftest LEGACY agent exists (last_seen_at NULL) -> no eligible target.
    with pytest.raises(NoActiveAgentError):
        await resolve_queue_for_task("process_file", app_state, session)


@pytest.mark.asyncio
async def test_resolve_unknown_task_raises_value_error() -> None:
    """An unknown task name fails loud -- it must never return the default queue."""
    app_state = stub_app_state()

    with pytest.raises(ValueError, match="unroutable task"):
        await resolve_queue_for_task("bogus_task", app_state, None)
