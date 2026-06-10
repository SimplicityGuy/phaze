"""Shared test doubles for Phase 30 control-plane queue routing tests.

Before this module, ``_FakeQueue`` / ``_FakeTaskRouter`` / ``_seed_active_agent`` /
``_stub_app_state`` / ``_wire_fakes`` were copy-pasted across six test modules
(``test_enqueue_router``, ``test_no_default_queue_producers``, ``test_pipeline``,
``test_pipeline_fingerprint``, ``test_tracklists``, ``test_scan``). Any change to
the fake queue interface meant chasing all six copies. They are consolidated here.

Two distinct shapes are provided, matching how the production code reads app state:

- :class:`FakeQueue` / :class:`FakeTaskRouter` — capturing doubles for the HTTP-level
  router tests. Every ``enqueue`` is recorded twice: as a ``(queue_name, task, kwargs)``
  triple on the router's shared ``captures`` list (so a test can assert the exact
  destination queue per enqueue), and as a ``(task, kwargs)`` pair on the queue's own
  ``captured`` list (so a test can assert what landed on one specific queue). ``enqueue``
  returns a ``MagicMock`` job exposing ``.key`` for callers that read it (the scan-poll
  flow), and ``job`` is an ``AsyncMock`` so ``queue.job(job_key)`` lookups are
  configurable. Callers that ignore the return value (the pipeline endpoints) are
  unaffected.
- :func:`stub_app_state` — a lightweight ``SimpleNamespace`` stub used by the unit tests
  that call :func:`phaze.services.enqueue_router.resolve_queue_for_task` directly without
  an HTTP client.

:func:`seed_active_agent` inserts a single non-revoked, recently-seen agent (committed,
per the WR-03 review note preferring a committed row over a flush-only one).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from phaze.models.agent import Agent


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


Capture = list[tuple[str, str, dict[str, Any]]]


class FakeQueue:
    """A named SAQ-queue stand-in that captures every enqueue.

    ``captured`` records ``(task_name, kwargs)`` pairs for this queue. When a shared
    ``capture`` list is supplied, each enqueue is *also* appended there as a
    ``(name, task_name, kwargs)`` triple so cross-queue assertions are possible.
    ``enqueue`` returns a ``MagicMock`` job whose ``.key`` is unique per call, and
    ``job`` is an ``AsyncMock`` (default returns ``None``) so the scan-status poll's
    queue-scoped ``queue.job(job_key)`` lookup can be configured.
    """

    def __init__(self, name: str, capture: Capture | None = None) -> None:
        self.name = name
        self._capture = capture
        self.captured: list[tuple[str, dict[str, Any]]] = []
        self.job = AsyncMock(return_value=None)
        self._counter = 0

    async def enqueue(self, task_name: str, **kwargs: Any) -> MagicMock:
        kw = dict(kwargs)
        if self._capture is not None:
            self._capture.append((self.name, task_name, kw))
        self.captured.append((task_name, kw))
        self._counter += 1
        job = MagicMock()
        job.key = f"{self.name}:job:{self._counter}"
        return job


class FakeTaskRouter:
    """An ``AgentTaskRouter`` stand-in: ``queue_for`` yields a ``phaze-agent-<id>`` queue.

    Queues are cached per ``agent_id`` (so a test can retrieve the same instance the
    handler used via ``router.queues[agent_id]``), and every ``queue_for`` argument is
    recorded on ``queue_for_calls``. All queues share the router's ``captures`` list.
    """

    def __init__(self, capture: Capture | None = None) -> None:
        self.captures: Capture = [] if capture is None else capture
        self.queues: dict[str, FakeQueue] = {}
        self.queue_for_calls: list[str] = []

    def queue_for(self, agent_id: str) -> FakeQueue:
        self.queue_for_calls.append(agent_id)
        if agent_id not in self.queues:
            self.queues[agent_id] = FakeQueue(f"phaze-agent-{agent_id}", self.captures)
        return self.queues[agent_id]


def install_fake_queues(client: AsyncClient) -> tuple[FakeQueue, FakeTaskRouter]:
    """Attach a fake controller queue + task_router to the test app state.

    Returns ``(controller_queue, task_router)`` so a test can assert on either the
    controller queue's ``captured`` list or the per-agent queues the router cached.
    The controller queue is independent (its own ``captured``); use this when a test
    inspects each double separately rather than a single merged capture list.
    """
    controller_queue = FakeQueue("controller")
    task_router = FakeTaskRouter()
    app = client._transport.app  # type: ignore[union-attr]
    app.state.controller_queue = controller_queue
    app.state.task_router = task_router
    return controller_queue, task_router


def wire_fakes(client: AsyncClient) -> Capture:
    """Attach fakes sharing one capture list; return that ``(name, task, kwargs)`` list.

    Use this when a test asserts the set/sequence of enqueues across *all* queues
    (controller + per-agent) from a single merged list.
    """
    router = FakeTaskRouter()
    capture = router.captures
    state = client._transport.app.state  # type: ignore[union-attr]
    state.controller_queue = FakeQueue("controller", capture)
    state.task_router = router
    return capture


def stub_app_state() -> SimpleNamespace:
    """app_state stub: a sentinel controller queue + a task_router whose
    ``queue_for(id)`` returns a per-id named sentinel (mirrors the real shapes).

    Used by the unit tests that drive ``resolve_queue_for_task`` directly, where no
    HTTP client / real queue is needed and only the queue *name* and ``agent_id`` are
    asserted.
    """
    controller_queue = SimpleNamespace(name="controller")

    class _StubRouter:
        def queue_for(self, agent_id: str) -> SimpleNamespace:
            return SimpleNamespace(name=f"phaze-agent-{agent_id}")

    return SimpleNamespace(controller_queue=controller_queue, task_router=_StubRouter())


async def seed_active_agent(session: AsyncSession, agent_id: str = "nox") -> Agent:
    """Insert one non-revoked, recently-seen agent so per-agent routing resolves it.

    Commits (and refreshes) the row — a committed agent is the canonical fixture per
    the WR-03 review note, over the flush-only variant.
    """
    agent = Agent(
        id=agent_id,
        name=agent_id,
        token_hash=None,
        scan_roots=[],
        last_seen_at=datetime.now(UTC),
        revoked_at=None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent
