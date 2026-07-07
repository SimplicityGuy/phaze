"""Controller-side per-agent SAQ enqueuer (Phase 26 D-19..D-21).

Replaces the inline ``Queue.from_url(...)`` + ``try/finally: queue.disconnect()``
pattern at agent_files.py:99-117 with a reusable service. Queue instances
are cached per-agent so the Redis connection pool is reused across enqueues.

Lifecycle: instantiated once in ``main.py``'s lifespan as ``app.state.task_router``;
shutdown calls ``close()`` to disconnect every cached queue.

Phase 26 D-18 + quick-260707-dh1: queue name format is
``phaze-agent-<agent_id>-<lane>`` where ``agent_id`` is the kebab-case slug from
Phase 24 D-01 (regex ``^[a-z0-9]+(-[a-z0-9]+)*$``) and ``<lane>`` is one of
``analyze`` / ``fingerprint`` / ``meta`` / ``io`` (``enqueue_router.LANES``). The
slug + lowercase lane guarantee Redis-safe key chars; no escaping needed.

This module is opted into mypy strict checking via the
``[[tool.mypy.overrides]] module = "phaze.services.agent_task_router"`` block in
pyproject.toml (Plan 01).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from phaze.services.enqueue_router import LANES, lane_for_task
from phaze.tasks._shared.queue_factory import build_pipeline_queue


if TYPE_CHECKING:
    from pydantic import BaseModel
    from saq import Queue
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from phaze.models.file import FileRecord


logger = structlog.get_logger(__name__)


class AgentTaskRouter:
    """Lazily-cached per-agent Queue enqueuer.

    Usage:
        router = AgentTaskRouter(queue_url=settings.queue_url, cache_redis_url=settings.redis_url)
        await router.enqueue_for_agent(
            agent_id="fileserver-01",
            task_name="extract_file_metadata",
            payload=ExtractMetadataPayload(file_id=..., ...),
        )
        # ... at shutdown:
        await router.close()

    Or via FileRecord:
        await router.enqueue_for_file(
            file_record=file_row,        # has .agent_id
            task_name="extract_file_metadata",
            payload=...,
        )

    Phase 30: :meth:`queue_for` exposes the cached per-agent Queue publicly so the
    shared ``phaze.services.enqueue_router.resolve_queue_for_task`` can route to the
    same hook-applied Queue instance without touching the private ``_queue_for``.
    """

    def __init__(self, queue_url: str, cache_redis_url: str, ledger_sessionmaker: async_sessionmaker | None = None) -> None:
        self._queue_url = queue_url
        self._cache_redis_url = cache_redis_url
        # Phase 45: OPTIONAL control-side scheduling-ledger sessionmaker. When provided, every
        # per-agent queue this router builds gets it attached so the before_enqueue WRITE hook
        # records the manual/recovery enqueue in the durable ledger. The control-side producers
        # (the API lifespan + the controller worker) pass it; nothing else does, so the agent
        # boundary is untouched.
        self._ledger_sessionmaker = ledger_sessionmaker
        self._queues: dict[tuple[str, str], Queue] = {}

    def queue_for(self, agent_id: str, lane: str) -> Queue:
        """Public accessor for the cached per-agent, per-LANE Queue (quick-260707-dh1).

        Returns the ``phaze-agent-<agent_id>-<lane>`` Queue (with the full
        before_enqueue hook chain already applied) so the shared
        ``enqueue_router.resolve_queue_for_task``, the depth readers, and every
        producer can fetch it without reaching into the private method.

        ``lane`` is REQUIRED -- there is NO silent default. A default would strand
        io tasks (s3_upload / push_file) on the analyze queue, whose worker registers
        only ``{process_file}`` (the Phase-30 / v4.0.8 stranding this design fixes),
        and would break the legacy-queue drain invariant. Producers resolve the lane
        via ``enqueue_router.lane_for_task(task_name)`` (fail-loud on an unmapped name).
        """
        return self._queue_for(agent_id, lane)

    def all_lane_queues(self, agent_id: str) -> list[Queue]:
        """Return the agent's four lane queues in ``enqueue_router.LANES`` order.

        The depth-reader helper (get_queue_activity, the /saq mount): a caller that
        needs the WHOLE agent's in-flight figure sums across every lane. Distinct from
        the producer path, which targets exactly one lane per task.
        """
        return [self._queue_for(agent_id, lane) for lane in LANES]

    def legacy_base_queue(self, agent_id: str) -> Queue:
        """Return the un-suffixed ``phaze-agent-<agent_id>`` queue -- READ-ONLY drain visibility.

        quick-260707-dh1: during the migration a transitional all-mode worker drains the legacy
        base queue. Depth readers (get_queue_activity, the /saq mount) include this queue so the
        drain window stays visible until it reports empty. This is the ONLY place a base
        (un-suffixed) queue is constructed, and it is NEVER a producer target -- lane routing is
        enforced everywhere jobs are enqueued (the no-default-queue regression asserts this).
        Cached under a sentinel lane key so it never collides with a real lane.
        """
        return self._queue_for(agent_id, "")

    def _queue_for(self, agent_id: str, lane: str) -> Queue:
        """Return the cached Queue for ``(agent_id, lane)``, constructing on first access.

        Queue name format is ``phaze-agent-<agent_id>-<lane>`` (quick-260707-dh1;
        base per Phase 26 D-18 + the lane suffix). Cached under ``(agent_id, lane)``.
        """
        cache_key = (agent_id, lane)
        # quick-260707-dh1: the sentinel empty lane is the READ-ONLY legacy base queue
        # (phaze-agent-<id>, no suffix) used only for drain visibility; every real lane suffixes.
        queue_name = f"phaze-agent-{agent_id}" if lane == "" else f"phaze-agent-{agent_id}-{lane}"
        if cache_key not in self._queues:
            # Phase 36: built via the single `build_pipeline_queue` seam -- a PostgresQueue
            # (broker = queue_url) with BOTH before_enqueue hooks (apply_project_job_defaults +
            # apply_deterministic_key) already registered and a decoupled `cache_redis` handle.
            # The factory owns the hook chain, so the per-agent dispatch path no longer registers
            # them inline (quick-260609-f96: this path once missed the defaults hook, giving
            # agent-dispatched scan_directory jobs SAQ's 10s default -- the factory now guarantees
            # both hooks on every queue). The hook chain + deterministic-key dedup are therefore
            # per-lane and identical. Conservative pool sizing (1/4) per lane keeps the per-queue
            # psycopg3 budget under Postgres max_connections (RESEARCH Pitfall 4).
            queue = build_pipeline_queue(
                queue_name,
                self._queue_url,
                cache_redis_url=self._cache_redis_url,
                min_size=1,
                max_size=4,
                ledger_sessionmaker=self._ledger_sessionmaker,
            )
            self._queues[cache_key] = queue
        return self._queues[cache_key]

    async def enqueue_for_agent(
        self,
        *,
        agent_id: str,
        task_name: str,
        payload: BaseModel,
        timeout: int | None = None,
        retries: int | None = None,
    ) -> Any:
        """Enqueue ``task_name`` with ``payload.model_dump()`` kwargs onto agent's queue.

        Returns the SAQ Job object from ``queue.enqueue(...)``. Callers may
        ignore the return; it is exposed for tests and instrumentation.

        ``timeout`` / ``retries`` are optional per-job overrides. When provided
        they are forwarded to ``queue.enqueue(...)`` as saq.Job dataclass fields
        (SAQ applies any kwarg matching a Job field as a Job property, not a
        function argument). When ``None`` (the default), neither key is passed,
        so the queue's ``apply_project_job_defaults`` before_enqueue hook applies
        the role's policy defaults. An explicit ``timeout=0`` disables the SAQ
        wall-clock timeout entirely (runs under ``wait_for(..., None)`` ->
        unbounded; ``Job.stuck`` stays False) -- used by ``scan_directory``,
        whose liveness is enforced by the progress-based stall reaper instead.
        ScanDirectoryPayload fields (scan_path/batch_id/agent_id) never collide
        with Job fields; the explicit overrides are merged last so they win.
        """
        # quick-260707-dh1: derive the lane from task_name (fail-loud on an unmapped name) so
        # existing callers (execution.py / pipeline_scans.py) stay untouched -- they pass
        # task_name and the lane is resolved here.
        lane = lane_for_task(task_name)
        queue = self._queue_for(agent_id, lane)
        # Phase 36: open the PostgresQueue broker pool (built open=False) before enqueueing.
        # connect() is idempotent (guarded by self._connected) -- a no-op after the first call.
        await queue.connect()
        dumped = payload.model_dump(mode="json")
        extra: dict[str, Any] = {}
        if timeout is not None:
            extra["timeout"] = timeout
        if retries is not None:
            extra["retries"] = retries
        # PR3: INFO so a real per-agent enqueue is visible in operational logs. file_id /
        # batch_id are bound when the payload carries them (None otherwise). No secrets.
        logger.info(
            "task enqueued",
            queue=f"phaze-agent-{agent_id}-{lane}",
            function=task_name,
            agent=agent_id,
            file_id=dumped.get("file_id"),
            batch_id=dumped.get("batch_id"),
            timeout=timeout,
            retries=retries,
        )
        return await queue.enqueue(task_name, **dumped, **extra)

    async def enqueue_for_file(
        self,
        *,
        file_record: FileRecord,
        task_name: str,
        payload: BaseModel,
        timeout: int | None = None,
        retries: int | None = None,
    ) -> Any:
        """Enqueue using ``file_record.agent_id`` (Phase 24 FK to agents.id).

        ``timeout`` / ``retries`` pass through to :meth:`enqueue_for_agent` for
        consistency; ``None`` (default) leaves the policy defaults in place.
        """
        return await self.enqueue_for_agent(
            agent_id=file_record.agent_id,
            task_name=task_name,
            payload=payload,
            timeout=timeout,
            retries=retries,
        )

    async def close(self) -> None:
        """Disconnect every cached Queue and clear the cache. Idempotent."""
        for queue in self._queues.values():
            # Phase 36 (WR-01): close the factory-attached cache_redis handle too —
            # disconnect() closes only the psycopg3 pool, leaving the Redis client open.
            cache_redis = getattr(queue, "cache_redis", None)
            if cache_redis is not None:
                await cache_redis.aclose()
            await queue.disconnect()
        self._queues.clear()
