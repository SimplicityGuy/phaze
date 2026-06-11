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

from saq import Job

from phaze.models.agent import Agent


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


Capture = list[tuple[str, str, dict[str, Any]]]


# Keys that ``saq.Queue.enqueue`` routes to the Job itself (timeout, retries, ttl,
# ...) rather than into ``job.kwargs`` (the task payload). Mirrors the real split in
# saq/queue/base.py: ``if k in Job.__dataclass_fields__``. Capturing these separately
# lets tests assert per-job control settings (Phase 31: process_file timeout/retries)
# without polluting the captured task payload that the worker would receive.
_JOB_CONTROL_FIELDS = frozenset(Job.__dataclass_fields__)


class FakeQueue:
    """A named SAQ-queue stand-in that captures every enqueue.

    ``captured`` records ``(task_name, kwargs)`` pairs for this queue, where ``kwargs``
    is the task *payload* only (job-control keys like ``timeout``/``retries`` are split
    out, mirroring ``saq.Queue.enqueue``). ``captured_policy`` records the per-enqueue
    job-control kwargs (``{"timeout": ..., "retries": ...}``) in parallel. When a shared
    ``capture`` list is supplied, each enqueue is *also* appended there as a
    ``(name, task_name, payload)`` triple so cross-queue assertions are possible.
    ``enqueue`` returns a ``MagicMock`` job whose ``.key`` is unique per call, and
    ``job`` is an ``AsyncMock`` (default returns ``None``) so the scan-status poll's
    queue-scoped ``queue.job(job_key)`` lookup can be configured.
    """

    def __init__(self, name: str, capture: Capture | None = None, *, queued: int = 0, active: int = 0, scheduled: int = 0) -> None:
        self.name = name
        self._capture = capture
        # Static QueueInfo counts surfaced by ``info`` (Phase 33 saq_web rendering).
        # Default to an idle queue (0/0/0); override to render non-zero job counts
        # in the dashboard under test without touching Redis.
        self._info_queued = queued
        self._info_active = active
        self._info_scheduled = scheduled
        self.captured: list[tuple[str, dict[str, Any]]] = []
        self.captured_policy: list[dict[str, Any]] = []
        self.job = AsyncMock(return_value=None)
        self._counter = 0
        # Per-kind queue depths read back by ``count`` (mirrors saq.Queue.count's
        # ``kind in {"queued", "active", "incomplete"}`` contract). Seed via
        # ``set_counts``; defaults to an idle queue so an un-seeded fake reads 0.
        self._counts: dict[str, int] = {"queued": 0, "active": 0, "incomplete": 0}
        # When True, ``count`` raises instead of returning — exercises the
        # ``get_queue_activity`` degrade-to-0 path (Plan 01) for a Redis outage.
        self._count_raises = False

    async def count(self, kind: str) -> int:
        """Return the seeded depth for ``kind`` (async, mirroring ``saq.Queue.count``).

        Raises ``RuntimeError`` when :meth:`fail_count` has been called, so a test can
        drive the queue-activity service's failure-isolation branch.
        """
        if self._count_raises:
            raise RuntimeError("fake redis down")
        return self._counts.get(kind, 0)

    def set_counts(self, *, queued: int = 0, active: int = 0, incomplete: int = 0) -> None:
        """Seed the per-kind depths this queue's :meth:`count` reads back."""
        self._counts["queued"] = queued
        self._counts["active"] = active
        self._counts["incomplete"] = incomplete

    def fail_count(self) -> None:
        """Make subsequent :meth:`count` calls raise, simulating a Redis outage."""
        self._count_raises = True

    async def enqueue(self, task_name: str, **kwargs: Any) -> MagicMock:
        # Mirror saq.Queue.enqueue: keys that are saq.Job dataclass fields are
        # job-control settings, not task kwargs. Split them so ``captured`` holds only
        # the payload the worker receives and ``captured_policy`` holds timeout/retries/etc.
        payload = {k: v for k, v in kwargs.items() if k not in _JOB_CONTROL_FIELDS}
        policy = {k: v for k, v in kwargs.items() if k in _JOB_CONTROL_FIELDS}
        if self._capture is not None:
            self._capture.append((self.name, task_name, payload))
        self.captured.append((task_name, payload))
        self.captured_policy.append(policy)
        self._counter += 1
        job = MagicMock()
        job.key = f"{self.name}:job:{self._counter}"
        return job

    async def info(self, jobs: bool = False, offset: int = 0, limit: int = 10) -> dict[str, Any]:  # noqa: ARG002
        """Return a Redis-free ``QueueInfo``-shaped mapping for ``saq_web`` rendering.

        Mirrors ``saq.queue.redis.Queue.info`` (saq/queue/redis.py:170-176): the six keys
        ``workers``/``name``/``queued``/``active``/``scheduled``/``jobs`` with ``name``
        echoing ``self.name``. ``workers`` and ``jobs`` are always empty (no live workers,
        no Redis); the three counts come from the constructor (default 0) so a test can
        render non-zero depths. ``saq_web`` passes ``jobs=True`` for the single-queue route
        and ``offset``/``limit`` for pagination — accepted for signature parity, unused
        here (``ARG002`` suppressed) because there are no real jobs to page over.
        """
        return {
            "workers": {},
            "name": self.name,
            "queued": self._info_queued,
            "active": self._info_active,
            "scheduled": self._info_scheduled,
            "jobs": [],
        }


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

    def set_counts(self, agent_id: str, *, queued: int = 0, active: int = 0) -> None:
        """Pre-seed a per-agent queue's depth before the service enumerates it.

        Forces lazy creation/caching of the ``phaze-agent-<id>`` fake via
        :meth:`queue_for`, then seeds its ``count`` depths — so a test can assert the
        summed agent depth a later ``queue_for`` read returns.
        """
        self.queue_for(agent_id).set_counts(queued=queued, active=active)


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
