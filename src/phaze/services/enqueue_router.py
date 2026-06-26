"""Shared enqueue-routing foundation (Phase 30 Plan 01).

Single source of truth that maps every control-plane task name to the SAQ queue an
actual worker consumes, so no API code path can enqueue onto the consumer-less
unnamed ``default`` queue (the v4.0.6 incident: 11,428 ``process_file`` jobs
stranded in ``saq:job:default:*``).

Two destinations:

- The named ``controller`` queue (``app.state.controller_queue``), consumed by the
  application-server ``phaze-worker`` (``phaze.tasks.controller.settings``).
- A per-agent ``phaze-agent-<id>`` queue (``AgentTaskRouter.queue_for(agent_id)``),
  consumed by a file-server ``phaze-agent-worker``
  (``phaze.tasks.agent_worker.settings``).

``CONTROLLER_TASKS`` and ``AGENT_TASKS`` MUST stay in sync with the ``functions``
lists registered in ``phaze.tasks.controller`` and ``phaze.tasks.agent_worker``
respectively. A task missing from both sets is unroutable and
:func:`resolve_queue_for_task` raises ``ValueError`` (fail loud, never silently
default).

Active-agent selection policy: :func:`select_active_agent` returns the
most-recently-seen non-revoked agent (``revoked_at IS NULL`` AND
``last_seen_at IS NOT NULL``, ORDER BY ``last_seen_at DESC`` LIMIT 1). This is the
simplest deterministic rule; round-robin / least-loaded dispatch is deferred. The
``revoked_at IS NULL`` predicate excludes the permanently-revoked
``legacy-application-server`` (its ``revoked_at`` equals its ``created_at``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import select

from phaze.models.agent import Agent


if TYPE_CHECKING:
    from saq import Queue
    from sqlalchemy.ext.asyncio import AsyncSession


CONTROLLER_TASKS: frozenset[str] = frozenset(
    {
        "generate_proposals",
        "search_tracklist",
        "scrape_and_store_tracklist",
        "match_tracklist_to_discogs",
        "refresh_tracklists",
    }
)
"""Fileless tasks the application-server controller worker consumes.

MUST mirror ``phaze.tasks.controller.settings["functions"]`` (+ the
``refresh_tracklists`` cron). ``reap_stalled_scans`` is cron-only (never
operator-enqueued) so it is intentionally omitted from the routable set.
"""

AGENT_TASKS: frozenset[str] = frozenset(
    {
        "process_file",
        "extract_file_metadata",
        "fingerprint_file",
        "scan_live_set",
        "scan_directory",
        "execute_approved_batch",
        "push_file",
    }
)
"""File-touching tasks a file-server agent worker consumes.

MUST mirror ``phaze.tasks.agent_worker.settings["functions"]`` (excluding the
``heartbeat_tick`` cron, which agents schedule for themselves and operators never
dispatch).
"""


class NoActiveAgentError(RuntimeError):
    """Raised when no eligible (non-revoked, recently-seen) agent exists."""


class RoutedQueue(NamedTuple):
    """The resolved destination for an enqueue: a queue + the selected agent.

    ``agent_id`` is ``None`` for controller tasks (the controller queue is not
    agent-scoped) and the chosen agent's id for per-agent tasks.
    """

    queue: Queue
    agent_id: str | None


async def select_active_agent(session: AsyncSession, kind: str | None = None) -> Agent:
    """Return the most-recently-seen non-revoked agent.

    Filters ``revoked_at IS NULL`` AND ``last_seen_at IS NOT NULL``, orders by
    ``last_seen_at DESC`` and takes the first row. The ``revoked_at IS NULL``
    predicate excludes ``legacy-application-server`` (permanently revoked).

    Phase 49 (D-13): when ``kind`` is given (``"compute"`` / ``"fileserver"``) the
    selection is scoped to agents of that ``Agent.kind`` — the deterministic
    most-recently-seen rule still holds, but only within the requested kind.
    ``kind=None`` preserves the original behavior (any kind), so every existing
    caller (e.g. :func:`resolve_queue_for_task`) is unchanged.

    Raises :class:`NoActiveAgentError` when no agent satisfies the filter — the
    consuming sites surface a clear error/empty-state instead of a silent success.
    """
    stmt = (
        select(Agent)
        .where(
            Agent.revoked_at.is_(None),
            Agent.last_seen_at.is_not(None),
        )
        .order_by(Agent.last_seen_at.desc())
        .limit(1)
    )
    if kind is not None:
        stmt = stmt.where(Agent.kind == kind)
    result = await session.execute(stmt)
    agent = result.scalar_one_or_none()
    if agent is None:
        msg = "no active agent available (all agents are revoked or have never checked in)"
        raise NoActiveAgentError(msg)
    return agent


async def resolve_queue_for_task(
    task_name: str,
    app_state: Any,
    session: AsyncSession | None,
) -> RoutedQueue:
    """Resolve ``task_name`` to the queue a real worker consumes.

    - ``task_name in CONTROLLER_TASKS`` -> ``app_state.controller_queue`` (agent_id None).
    - ``task_name in AGENT_TASKS`` -> the per-agent queue for the selected active
      agent. Requires ``session`` (raises ``ValueError`` if ``None``); the agent is
      chosen via :func:`select_active_agent` (which may raise
      :class:`NoActiveAgentError`).
    - anything else -> ``ValueError`` (fail loud; never returns the default queue).
    """
    if task_name in CONTROLLER_TASKS:
        queue = app_state.controller_queue
        # Phase 36: open the PostgresQueue broker pool (built open=False) before the caller
        # enqueues. connect() is idempotent (guarded by self._connected) -- a no-op after the
        # first call. Single chokepoint so every routed.queue.enqueue(...) site (and the
        # background tasks that receive routed.queue) finds an open pool.
        await queue.connect()
        return RoutedQueue(queue, None)
    if task_name in AGENT_TASKS:
        if session is None:
            msg = f"resolving per-agent task {task_name!r} requires a database session"
            raise ValueError(msg)
        agent = await select_active_agent(session)
        queue = app_state.task_router.queue_for(agent.id)
        await queue.connect()
        return RoutedQueue(queue, agent.id)
    msg = f"unroutable task: {task_name}"
    raise ValueError(msg)
