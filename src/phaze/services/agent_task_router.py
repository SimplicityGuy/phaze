"""Controller-side per-agent SAQ enqueuer (Phase 26 D-19..D-21).

Replaces the inline ``Queue.from_url(...)`` + ``try/finally: queue.disconnect()``
pattern at agent_files.py:99-117 with a reusable service. Queue instances
are cached per-agent so the Redis connection pool is reused across enqueues.

Lifecycle: instantiated once in ``main.py``'s lifespan as ``app.state.task_router``;
shutdown calls ``close()`` to disconnect every cached queue.

Phase 26 D-18: queue name format is exactly ``phaze-agent-<agent_id>`` where
``agent_id`` is the kebab-case slug from Phase 24 D-01 (regex
``^[a-z0-9]+(-[a-z0-9]+)*$``). The slug guarantees Redis-safe key chars; no
escaping needed.

This module is opted into mypy strict checking via the
``[[tool.mypy.overrides]] module = "phaze.services.agent_task_router"`` block in
pyproject.toml (Plan 01).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from saq import Queue
import structlog

from phaze.tasks._shared.queue_defaults import apply_project_job_defaults


if TYPE_CHECKING:
    from pydantic import BaseModel

    from phaze.models.file import FileRecord


logger = structlog.get_logger(__name__)


class AgentTaskRouter:
    """Lazily-cached per-agent Queue enqueuer.

    Usage:
        router = AgentTaskRouter(redis_url=settings.redis_url)
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
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._queues: dict[str, Queue] = {}

    def _queue_for(self, agent_id: str) -> Queue:
        """Return the cached Queue for ``agent_id``, constructing on first access.

        Queue name format is ``phaze-agent-<agent_id>`` per Phase 26 D-18.
        """
        if agent_id not in self._queues:
            queue = Queue.from_url(
                self._redis_url,
                name=f"phaze-agent-{agent_id}",
            )
            # Phase 27 UAT Gap 1: SAQ 0.26.3's Worker.__init__ does NOT accept `timeout`,
            # `retries`, or `keep_result` -- those are per-Job settings. Apply the project's
            # policy defaults via a `before_enqueue` hook on the Queue so every enqueued
            # Job inherits the longer timeout / retry budget without breaking Worker
            # construction. See phaze.tasks._shared.queue_defaults for the hook body.
            # quick-260609-f96: the controller (controller.py) and agent worker (agent_worker.py)
            # both register this hook, but this per-agent dispatch path did not -- so
            # agent-dispatched jobs (notably scan_directory) inherited SAQ's 10s default
            # and were cancelled with asyncio.TimeoutError after exactly 10s. Register once
            # here, only on first construction per agent_id (never re-registered on cache hits).
            queue.register_before_enqueue(apply_project_job_defaults)
            self._queues[agent_id] = queue
        return self._queues[agent_id]

    async def enqueue_for_agent(
        self,
        *,
        agent_id: str,
        task_name: str,
        payload: BaseModel,
    ) -> Any:
        """Enqueue ``task_name`` with ``payload.model_dump()`` kwargs onto agent's queue.

        Returns the SAQ Job object from ``queue.enqueue(...)``. Callers may
        ignore the return; it is exposed for tests and instrumentation.
        """
        queue = self._queue_for(agent_id)
        dumped = payload.model_dump(mode="json")
        # PR3: INFO so a real per-agent enqueue is visible in operational logs. file_id /
        # batch_id are bound when the payload carries them (None otherwise). No secrets.
        logger.info(
            "task enqueued",
            queue=f"phaze-agent-{agent_id}",
            function=task_name,
            agent=agent_id,
            file_id=dumped.get("file_id"),
            batch_id=dumped.get("batch_id"),
        )
        return await queue.enqueue(task_name, **dumped)

    async def enqueue_for_file(
        self,
        *,
        file_record: FileRecord,
        task_name: str,
        payload: BaseModel,
    ) -> Any:
        """Enqueue using ``file_record.agent_id`` (Phase 24 FK to agents.id)."""
        return await self.enqueue_for_agent(
            agent_id=file_record.agent_id,
            task_name=task_name,
            payload=payload,
        )

    async def close(self) -> None:
        """Disconnect every cached Queue and clear the cache. Idempotent."""
        for queue in self._queues.values():
            await queue.disconnect()
        self._queues.clear()
