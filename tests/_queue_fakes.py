"""Shared test doubles for Phase 30 control-plane queue routing tests.

Before this module, ``_FakeQueue`` / ``_FakeTaskRouter`` / ``_seed_active_agent`` /
``_stub_app_state`` / ``_wire_fakes`` were copy-pasted across six test modules
(``test_enqueue_router``, ``test_no_default_queue_producers``, ``test_pipeline``,
``test_pipeline``, ``test_tracklists``, ``test_scan``). Any change to
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
import functools
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from saq import Job
from saq.queue.postgres import PostgresQueue
from sqlalchemy import update

from phaze.models.agent import Agent
from phaze.services.enqueue_router import LANES, lane_for_task
from tests.db_guard import integration_dsns


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

# --- phaze-9nz1g (seam E1): the REAL serializer, on a never-connected PostgresQueue ---------
#
# Before this, ``FakeQueue.enqueue`` recorded the kwargs dict IN-PROCESS and UNSERIALIZED, so the
# type-fidelity property of every ``queue.enqueue`` -> broker -> worker ``**kwargs`` hop was
# asserted NOWHERE: a UUID / datetime / Path / enum kwarg that the real broker rejects with a
# ``TypeError``, and a tuple / int-keyed dict that survives but comes back a DIFFERENT TYPE, both
# read as a clean pass. That is ADR-0012 rule 3 verbatim -- the producer's own in-process echo is
# not the artifact's real consumer.
#
# The fix routes every fake enqueue through the SAME code the production broker runs:
# ``PostgresQueue.serialize`` (``json.dumps(job.to_dict())`` -- ``build_pipeline_queue`` registers
# no custom ``dump``/``load``, so SAQ's defaults are what ships) and ``PostgresQueue.deserialize``
# (``json.loads`` -> ``Job(**job_dict)``). NOT a hand-rolled ``json.dumps`` standing in for it:
# a stand-in would drift the moment a custom serializer is configured, which is the whole failure
# mode this seam exists to close.
#
# The queue is NEVER connected. ``PostgresQueue.__init__`` builds its psycopg pool ``open=False``
# and ``serialize``/``deserialize`` touch only ``self._dump`` / ``self._load`` / ``self.name``, so
# constructing one opens no socket and needs no live Postgres -- these stay usable in the hermetic
# unit suite. The DSN comes from ``tests.db_guard.integration_dsns`` rather than a literal purely so
# no second, independently-hardcoded DSN default can drift from the guard (phaze-yxklc).
_SERIALIZER_DSN, _ = integration_dsns()


@functools.cache
def _serializer_queue(name: str) -> PostgresQueue:
    """Return a cached, NEVER-CONNECTED ``PostgresQueue`` named ``name``, for serialize/deserialize only.

    DO NOT "simplify" this to a bare ``json.dumps``/``json.loads`` pair, and do not connect it.
    Both halves of that sentence are load-bearing, and neither is obvious from the call site:

    * It must be a REAL ``PostgresQueue`` because the property under test is "the fake agrees with
      the production broker". A hand-rolled ``json.dumps`` agrees with today's broker by
      coincidence and would silently stop agreeing the day ``build_pipeline_queue`` passes a custom
      ``dump``/``load`` -- which is exactly the drift this seam exists to prevent (ADR-0012 rule 3:
      verify with the artifact's real consumer, not with a stand-in for it).
    * It is never connected, and needs no live Postgres, because ``PostgresQueue.__init__`` builds
      its psycopg pool ``open=False`` and ``serialize``/``deserialize`` touch only ``self._dump`` /
      ``self._load`` / ``self.name``. That is what lets the REAL serializer run inside the hermetic
      unit suite -- no socket, no fixture, no skip-when-the-broker-is-down. A "cleanup" that awaits
      ``connect()`` here would make several hundred hermetic tests require a database.

    ``tests/shared/test_fake_queue_serializer_fidelity.py`` asserts both properties, so a change
    that breaks either goes red rather than quietly degrading the check.

    Cached per queue name because ``deserialize`` refuses a blob whose embedded ``queue`` field does
    not match ``self.name`` (``saq/queue/base.py``), so the round trip has to run on a queue carrying
    the same name as the fake it is standing behind.
    """
    return PostgresQueue(url=_SERIALIZER_DSN, name=name)


class NonSerializableEnqueueError(TypeError):
    """A fake enqueue carried a payload the REAL Postgres broker could not serialize (phaze-9nz1g).

    A ``TypeError`` subclass because that is exactly what ``json.dumps`` -- and therefore
    ``PostgresQueue.enqueue`` -- raises in production for the same value; the subclass only adds a
    message naming the offending queue/task and the fix. Seeing this from a test means the test was
    passing a value no real enqueue could ever have carried: serialize it at the producer
    (``payload.model_dump(mode="json")``, ``str(some_uuid)``) exactly as production does.
    """


def round_trip_enqueue_kwargs(
    queue_name: str,
    task_name: str,
    payload: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Round-trip one enqueue's kwargs through the real SAQ ``PostgresQueue`` serializer.

    Builds the ``saq.Job`` the real ``Queue.enqueue`` would build from the same split
    (payload -> ``job.kwargs``, job-control keys -> ``Job`` fields), serializes it, deserializes it,
    and returns ``(worker_visible_payload, worker_visible_policy)`` -- what the WORKER would receive,
    not what the producer handed in. The two differ precisely where this seam had no coverage:
    a ``tuple`` comes back a ``list``, an int-keyed ``dict`` comes back string-keyed, and a
    ``UUID`` / ``datetime`` / ``Path`` / ``enum`` / ``set`` / ``Decimal`` does not come back at all.

    ``queue`` is dropped from ``policy`` before constructing the Job (it IS a ``Job`` dataclass field,
    so the caller's split routes it here, but the real ``Queue.enqueue`` overwrites it with the
    enqueueing queue on the very next line -- so the fake must too, or the round trip would
    deserialize against the wrong name).

    Raises:
        NonSerializableEnqueueError: the payload or policy is not JSON-serializable, i.e. the real
            broker would have raised ``TypeError`` here too.
    """
    queue = _serializer_queue(queue_name)
    job_fields = {k: v for k, v in policy.items() if k != "queue"}
    job = Job(function=task_name, kwargs=dict(payload), queue=queue, **job_fields)
    try:
        blob = queue.serialize(job)
    except TypeError as exc:
        raise NonSerializableEnqueueError(
            f"enqueue({task_name!r}) on queue {queue_name!r} carried a payload the real Postgres broker "
            f"cannot serialize: {exc}. Production serializes at the producer -- "
            f'`payload.model_dump(mode="json")` or `str(value)` -- so a test passing the raw object is '
            f"exercising a hop that could never happen. Payload keys: {sorted(payload)}."
        ) from exc
    restored = queue.deserialize(blob)
    assert restored is not None  # a serialized Job never deserializes to None (only an empty blob does)
    # ``Job.to_dict`` omits any field still at its dataclass default, so read the policy back off the
    # restored Job by attribute rather than out of the blob -- an explicitly-passed default (say
    # ``retries=1``) must still be reported as captured.
    return dict(restored.kwargs or {}), {k: getattr(restored, k) for k in job_fields}


class FakeRedis:
    """A minimal in-memory async Redis double for the maintained pipeline counters.

    Implements only the surface ``phaze.services.pipeline_counters`` touches: ``incr``
    (durable INCR, returns the new value) and ``mget`` (returns ``bytes`` per present key
    or ``None`` for a miss, mirroring a non-``decode_responses`` client so the service's
    ``_to_int`` bytes-decode path is exercised). Phase 36: attach to a :class:`FakeQueue` /
    :class:`DedupFakeQueue` via its ``cache_redis`` attribute so the ``before_enqueue`` key
    hook's best-effort counter INCR (which reads ``job.queue.cache_redis``, NOT the removed
    ``job.queue.redis`` -- the broker is Postgres now) lands somewhere assertable.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        return [str(self.store[k]).encode() if k in self.store else None for k in keys]

    async def aclose(self) -> None:
        # Phase 36 (WR-01): shutdown paths close the factory-attached cache_redis handle.
        return None


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
        # Phase 36: decoupled cache-redis handle the counter hooks read via
        # ``getattr(job.queue, "cache_redis", None)`` (the broker is Postgres now, so there is
        # no ``queue.redis``). A real FakeRedis so a test driving the hooks finds it assertable.
        self.cache_redis = FakeRedis()
        self.job = AsyncMock(return_value=None)
        self._counter = 0
        # Per-kind queue depths read back by ``count`` (mirrors saq.Queue.count's
        # ``kind in {"queued", "active", "incomplete"}`` contract). Seed via
        # ``set_counts``; defaults to an idle queue so an un-seeded fake reads 0.
        self._counts: dict[str, int] = {"queued": 0, "active": 0, "incomplete": 0}
        # When True, ``count`` raises instead of returning — exercises the
        # ``get_queue_activity`` degrade-to-0 path (Plan 01) for a Redis outage.
        self._count_raises = False
        # Models the real ``PostgresQueue`` whose pool is built ``open=False``: ``count``
        # raises until ``connect()`` opens it. Off by default (the existing consumers never
        # connect); opt in via :meth:`require_connect` to drive the queue-activity
        # connect-before-count path for a runtime-registered agent (one not pre-connected at
        # startup by ``main.py``'s lifespan). (#217)
        self._connected = False
        self._needs_connect = False

    async def connect(self) -> None:
        """Stand-in for ``saq.Queue.connect`` (Phase 36); idempotent, marks connected.

        Producer paths now call ``await queue.connect()`` to open the PostgresQueue's
        psycopg pool before enqueueing (the real pool is built ``open=False``). The fake
        holds no real connection, so connecting only flips ``_connected`` -- enough for a
        test to assert a reader connected before counting (and a no-op on repeat calls,
        mirroring the real ``if self._connected: return`` guard).
        """
        self._connected = True

    def require_connect(self) -> FakeQueue:
        """Make :meth:`count` raise until :meth:`connect` is called; returns self to chain.

        Models a ``PostgresQueue`` whose pool was never opened -- the state of a per-agent
        queue for an agent registered *after* startup (``main.py`` only pre-connects the
        agents present at boot). Lets a test assert ``get_queue_activity`` connects the
        queue before counting instead of degrading the whole agent source to 0. (#217)
        """
        self._needs_connect = True
        return self

    async def count(self, kind: str) -> int:
        """Return the seeded depth for ``kind`` (async, mirroring ``saq.Queue.count``).

        Raises ``RuntimeError`` when :meth:`fail_count` has been called (Redis outage) or
        when :meth:`require_connect` is armed and :meth:`connect` has not run (an unopened
        psycopg pool), so a test can drive the queue-activity service's failure-isolation
        and connect-before-count branches.
        """
        if self._needs_connect and not self._connected:
            raise RuntimeError("the pool 'fake' is not open yet")
        if self._count_raises:
            raise RuntimeError("fake redis down")
        return self._counts.get(kind, 0)

    def set_counts(self, *, queued: int = 0, active: int = 0, incomplete: int = 0) -> FakeQueue:
        """Seed the per-kind depths this queue's :meth:`count` reads back; returns self to chain."""
        self._counts["queued"] = queued
        self._counts["active"] = active
        self._counts["incomplete"] = incomplete
        return self

    def fail_count(self) -> FakeQueue:
        """Make subsequent :meth:`count` calls raise, simulating a Redis outage; returns self."""
        self._count_raises = True
        return self

    async def enqueue(self, task_name: str, **kwargs: Any) -> MagicMock:
        # Mirror saq.Queue.enqueue: keys that are saq.Job dataclass fields are
        # job-control settings, not task kwargs. Split them so ``captured`` holds only
        # the payload the worker receives and ``captured_policy`` holds timeout/retries/etc.
        payload = {k: v for k, v in kwargs.items() if k not in _JOB_CONTROL_FIELDS}
        policy = {k: v for k, v in kwargs.items() if k in _JOB_CONTROL_FIELDS}
        # phaze-9nz1g: record what the WORKER would receive, not what the producer handed in --
        # the real ``PostgresQueue`` serializer stands between the two. See
        # :func:`round_trip_enqueue_kwargs`. A non-serializable value raises here, exactly as the
        # real broker would, instead of being echoed back unchanged.
        payload, policy = round_trip_enqueue_kwargs(self.name, task_name, payload, policy)
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


def _lane_key(agent_id: str, lane: str | None) -> str:
    """Cache key for a per-agent, per-lane fake queue.

    ``lane=None`` / ``""`` (the legacy base) maps to the bare ``agent_id`` key so a test
    can still read the base queue via ``router.queues[agent_id]``; a real lane appends
    ``-<lane>`` (e.g. ``"nox-analyze"``), mirroring the ``phaze-agent-<id>-<lane>`` naming.
    """
    return agent_id if lane in (None, "") else f"{agent_id}-{lane}"


def _lane_name(agent_id: str, lane: str | None) -> str:
    """Queue name for a per-agent, per-lane fake queue (base when lane is None/"")."""
    return f"phaze-agent-{agent_id}" if lane in (None, "") else f"phaze-agent-{agent_id}-{lane}"


class FakeTaskRouter:
    """An ``AgentTaskRouter`` stand-in: ``queue_for`` yields a ``phaze-agent-<id>-<lane>`` queue.

    quick-260707-dh1: ``queue_for(agent_id, lane)`` is lane-aware. Queues are cached under
    ``_lane_key`` (``"nox-analyze"`` for a lane, bare ``"nox"`` for the legacy base) so a test
    retrieves the same instance the handler used via ``router.queues["nox-analyze"]``. ``lane``
    is optional on the fake (defaults to the base) purely for test ergonomics; the REAL router
    requires it. Every ``queue_for`` records the ``agent_id`` on ``queue_for_calls``. All queues
    share the router's ``captures`` list. ``all_lane_queues`` / ``legacy_base_queue`` mirror the
    depth-reader helpers.
    """

    def __init__(self, capture: Capture | None = None) -> None:
        self.captures: Capture = [] if capture is None else capture
        self.queues: dict[str, FakeQueue] = {}
        self.queue_for_calls: list[str] = []

    def queue_for(self, agent_id: str, lane: str | None = None) -> FakeQueue:
        self.queue_for_calls.append(agent_id)
        key = _lane_key(agent_id, lane)
        if key not in self.queues:
            self.queues[key] = FakeQueue(_lane_name(agent_id, lane), self.captures)
        return self.queues[key]

    def all_lane_queues(self, agent_id: str) -> list[FakeQueue]:
        return [self.queue_for(agent_id, lane) for lane in LANES]

    def legacy_base_queue(self, agent_id: str) -> FakeQueue:
        return self.queue_for(agent_id, "")

    async def enqueue_for_agent(
        self,
        *,
        agent_id: str,
        task_name: str,
        payload: Any,
        timeout: int | None = None,
        retries: int | None = None,
    ) -> Any:
        """Mirror the real router's producer entry point: resolve the LANE, then enqueue.

        phaze-6bkk: added because the tag-write and CUE-generate routes are producers now (the api
        container has no media mount, so it dispatches instead of writing). Resolving the lane via
        the SAME ``lane_for_task`` the real router uses means a test that forgets to register a new
        task in ``LANE_TASKS`` fails HERE, loudly, instead of silently exercising a queue no worker
        consumes.
        """
        lane = lane_for_task(task_name)
        queue = self.queue_for(agent_id, lane)
        await queue.connect()
        extra: dict[str, Any] = {}
        if timeout is not None:
            extra["timeout"] = timeout
        if retries is not None:
            extra["retries"] = retries
        return await queue.enqueue(task_name, **payload.model_dump(mode="json"), **extra)

    async def enqueue_for_file(
        self,
        *,
        file_record: Any,
        task_name: str,
        payload: Any,
        timeout: int | None = None,
        retries: int | None = None,
    ) -> Any:
        """Delegate to :meth:`enqueue_for_agent` using ``file_record.agent_id`` (phaze-c9w9 affinity)."""
        return await self.enqueue_for_agent(
            agent_id=file_record.agent_id,
            task_name=task_name,
            payload=payload,
            timeout=timeout,
            retries=retries,
        )

    def set_counts(self, agent_id: str, *, lane: str | None = None, queued: int = 0, active: int = 0) -> None:
        """Pre-seed a per-agent (per-lane, or base) queue's depth before the service enumerates it.

        Forces lazy creation/caching of the ``phaze-agent-<id>[-<lane>]`` fake via
        :meth:`queue_for`, then seeds its ``count`` depths — so a test can assert the
        summed agent depth a later read returns. ``lane=None`` seeds the legacy base queue,
        which ``get_queue_activity`` includes for drain visibility.
        """
        self.queue_for(agent_id, lane).set_counts(queued=queued, active=active)


class DedupFakeQueue(FakeQueue):
    """A :class:`FakeQueue` that models SAQ's deterministic-key dedup no-op.

    Real ``saq.queue.redis.Queue._enqueue`` checks whether the job's id (which embeds
    the deterministic ``key``) is already a member of the per-queue ``incomplete`` sorted
    set; if so its Lua script returns nil and ``Queue.enqueue`` returns ``None`` — a clean
    no-op (no raise, no overwrite, the payload never lands). The key leaves ``incomplete``
    only when the job finishes, after which the same key may enqueue again (32-RESEARCH
    §Q1). The base :class:`FakeQueue` cannot express this: it always appends and returns a
    fresh ``MagicMock`` job (§Q5 Wave-0 gap).

    This subclass is purely additive — :class:`FakeQueue` itself is untouched, so the six
    existing consumers stay byte-identical in behavior. A ``key`` kwarg is a ``saq.Job``
    dataclass field (``key in _JOB_CONTROL_FIELDS``), so the parent already routes it into
    ``captured_policy`` rather than the payload; here we additionally use it as the dedup
    discriminator. A keyless enqueue never dedups (it always returns a job), preserving
    today's default-uuid producers.
    """

    def __init__(self, name: str, capture: Capture | None = None, *, queued: int = 0, active: int = 0, scheduled: int = 0) -> None:
        super().__init__(name, capture, queued=queued, active=active, scheduled=scheduled)
        # Deterministic keys currently "incomplete" (queued or active). A repeat enqueue
        # of a member is a no-op; ``finish`` removes a key to model job completion.
        self._live_keys: set[str] = set()

    async def enqueue(self, task_name: str, **kwargs: Any) -> MagicMock | None:  # type: ignore[override]
        # A ``key`` kwarg lands in the job-control fields (``key in _JOB_CONTROL_FIELDS``),
        # mirroring how the parent splits kwargs. Use it as the dedup discriminator.
        key = kwargs.get("key")
        if key is not None and key in self._live_keys:
            # Deduped: SAQ returns None and the payload never lands — do NOT append to
            # ``captured``/``captured_policy``/the shared ``_capture`` list.
            return None
        job = await super().enqueue(task_name, **kwargs)
        if key is not None:
            self._live_keys.add(key)
        return job

    def finish(self, key: str) -> None:
        """Discard ``key`` from the live set so a test can model job completion.

        After ``finish(key)`` the same deterministic key re-enqueues and returns a job,
        mirroring SAQ removing a finished job's id from the ``incomplete`` set.
        """
        self._live_keys.discard(key)


class DedupFakeTaskRouter(FakeTaskRouter):
    """A :class:`FakeTaskRouter` whose ``queue_for`` caches :class:`DedupFakeQueue` instances.

    Identical wiring to the parent (``queue_for_calls`` recording, shared ``captures``
    list, per-agent caching) — the only override is constructing a :class:`DedupFakeQueue`
    so per-agent queues model SAQ's deterministic-key dedup.
    """

    def __init__(self, capture: Capture | None = None) -> None:
        super().__init__(capture)
        self.queues: dict[str, DedupFakeQueue] = {}  # type: ignore[assignment]

    def queue_for(self, agent_id: str, lane: str | None = None) -> DedupFakeQueue:  # type: ignore[override]
        self.queue_for_calls.append(agent_id)
        key = _lane_key(agent_id, lane)
        if key not in self.queues:
            self.queues[key] = DedupFakeQueue(_lane_name(agent_id, lane), self.captures)
        return self.queues[key]


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
    asserted. Phase 36: each sentinel queue carries a no-op async ``connect`` because
    ``resolve_queue_for_task`` now opens the PostgresQueue pool before returning.
    """
    controller_queue = SimpleNamespace(name="controller", connect=AsyncMock())

    class _StubRouter:
        def queue_for(self, agent_id: str, lane: str | None = None) -> SimpleNamespace:
            return SimpleNamespace(name=_lane_name(agent_id, lane), connect=AsyncMock())

        def all_lane_queues(self, agent_id: str) -> list[SimpleNamespace]:
            return [self.queue_for(agent_id, lane) for lane in LANES]

        def legacy_base_queue(self, agent_id: str) -> SimpleNamespace:
            return self.queue_for(agent_id, "")

    return SimpleNamespace(controller_queue=controller_queue, task_router=_StubRouter())


async def make_agent_live(session: AsyncSession, agent_id: str = "test-fileserver") -> None:
    """Stamp ``last_seen_at`` on an EXISTING agent row so ownership routing resolves it.

    phaze-c9w9: file-keyed agent tasks route to the FILE's owning agent
    (``FileRecord.agent_id``) iff that agent is live -- never to the most-recently-seen
    fileserver. ``make_file`` owns its rows to the conftest-seeded ``test-fileserver``
    FK parent, which is deliberately never-seen (``last_seen_at IS NULL``) so the
    no-active-agent paths stay testable. Tests that expect a file-keyed enqueue to
    ROUTE call this to bring the owner live (the update happens inside the per-test
    transaction, so hermeticity is preserved).
    """
    await session.execute(update(Agent).where(Agent.id == agent_id).values(last_seen_at=datetime.now(UTC)))
    await session.commit()


async def seed_active_agent(session: AsyncSession, agent_id: str = "nox", *, kind: str = "fileserver") -> Agent:
    """Insert one non-revoked, recently-seen agent so per-agent routing resolves it.

    Commits (and refreshes) the row — a committed agent is the canonical fixture per
    the WR-03 review note, over the flush-only variant.

    Phase 49 (RESEARCH A3 / Wave 0): ``kind`` lets a test seed a ``"compute"`` (cloud)
    agent so the kind-filtered ``select_active_agent`` can be exercised. Defaults to
    ``"fileserver"`` so every existing caller is unchanged.
    """
    agent = Agent(
        id=agent_id,
        name=agent_id,
        token_hash=None,
        kind=kind,
        scan_roots=[],
        last_seen_at=datetime.now(UTC),
        revoked_at=None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent
