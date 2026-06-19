"""Single ``PostgresQueue`` construction seam for the pipeline (Phase 36).

Every queue construction site in the codebase adopts :func:`build_pipeline_queue` (Plan 02)
so the queue backend choice, pool sizing, and the project's before-enqueue hook chain live
in exactly ONE place. This decouples the migration from Redis to Postgres from every call
site: swapping ``RedisQueue`` for ``PostgresQueue`` happens here, not at N scattered sites.

What the factory guarantees on every returned queue:

* it is a :class:`saq.queue.postgres.PostgresQueue` (raw libpq DSN broker);
* all three backend-agnostic ``before_enqueue`` hooks are registered — :func:`apply_project_job_defaults`
  (Phase 27 job-policy defaults), :func:`apply_deterministic_key` (Phase 35 anti-drift key
  + enqueued counter), and :func:`apply_stage_control` (Phase 37 per-stage priority stamp +
  pause park). These hooks live on the base :class:`saq.Queue`, so they carry over to
  the Postgres backend unchanged;
* a dedicated ``cache_redis`` handle is attached. The counter hooks read the Redis client off
  ``getattr(job.queue, "cache_redis", None)`` (the ``before_enqueue`` callback only receives
  ``job``, so the client must hang off the queue object). NOTHING reads ``queue.redis`` —
  decoupling the cache from the now-Postgres broker is the whole point of this seam
  (36-RESEARCH Open Q3, LOCKED).

Construction opens NO connection: ``PostgresQueue.from_url`` builds its ``AsyncConnectionPool``
with ``open=False``, and ``redis.asyncio.Redis.from_url`` is lazy. The pool is opened later in
the role startup (Plan 02), never here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.asyncio as aioredis
from saq.queue.postgres import PostgresQueue
import structlog

from phaze.tasks._shared.deterministic_key import apply_deterministic_key
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults
from phaze.tasks._shared.stage_control import apply_stage_control


if TYPE_CHECKING:
    # TYPE-ONLY: keep ``sqlalchemy.ext.asyncio`` OUT of the runtime import graph. This module is
    # built by the AGENT worker too (its queue), which must stay Postgres-free (test_task_split).
    # The ledger_sessionmaker param is None on the agent path, so no async_sessionmaker is needed
    # at runtime there.
    from sqlalchemy.ext.asyncio import async_sessionmaker


logger = structlog.get_logger(__name__)


def build_pipeline_queue(
    name: str,
    url: str,
    *,
    cache_redis_url: str,
    min_size: int = 1,
    max_size: int = 4,
    ledger_sessionmaker: async_sessionmaker | None = None,
) -> PostgresQueue:
    """Construct the project's ``PostgresQueue`` with both hooks + a decoupled cache handle.

    Args:
        name: queue name (e.g. ``"controller"`` or ``"phaze-agent-<host>"``).
        url: raw libpq DSN (``postgresql://...``) — already driver-stripped by
            ``config.BaseSettings._strip_sqlalchemy_driver``. psycopg3 cannot parse the
            ``+asyncpg`` dialect form.
        cache_redis_url: Redis DSN for the ``cache_redis`` handle the counter hooks read.
        min_size: psycopg3 pool minimum connections (default 1).
        max_size: psycopg3 pool maximum connections (default 4).
        ledger_sessionmaker: OPTIONAL control-side ``async_sessionmaker``. When provided it is
            attached to the queue as ``q.ledger_sessionmaker`` so the ledger before_enqueue /
            after_process hooks can WRITE / CLEAR. Omit it (the default) on the agent worker
            queue to keep that queue Postgres-free (the hooks degrade to a logged no-op).

    Returns:
        A :class:`PostgresQueue` with ``apply_project_job_defaults``,
        ``apply_deterministic_key``, and ``apply_stage_control`` registered as
        ``before_enqueue`` callbacks and a ``cache_redis`` attribute set. No connection is
        opened at construction.
    """
    q = PostgresQueue.from_url(url, name=name, min_size=min_size, max_size=max_size)
    q.register_before_enqueue(apply_project_job_defaults)
    q.register_before_enqueue(apply_deterministic_key)
    # Phase 37: stamp per-stage priority + park paused stage jobs. Registered THIRD, AFTER
    # apply_deterministic_key, so job.function/job.key are final when the hook resolves the
    # stage and reads the control table via this queue's psycopg3 pool (TTL-cached). All four
    # construction sites inherit it through this single seam; the agent worker registers it
    # too but never enqueues stage jobs, and the hook stays DB-layer-free (T-37-04).
    q.register_before_enqueue(apply_stage_control)
    # Dedicated cache handle: the counter hooks read this off the queue object, never
    # ``queue.redis`` (the broker is Postgres now). ``from_url`` is lazy — no socket opens.
    # ``cache_redis`` is a dynamic attribute the counter hooks read via getattr; SAQ's
    # PostgresQueue does not declare it, so the assignment needs an attr-defined ignore.
    q.cache_redis = aioredis.Redis.from_url(cache_redis_url)  # type: ignore[attr-defined]
    # Phase 45: attach the control-side scheduling-ledger sessionmaker when provided (symmetric
    # with cache_redis -- a dynamic attr the before_enqueue / after_process hooks read via
    # getattr). Provided ONLY on the control-side queues (controller + per-agent router queues);
    # omitted on the agent worker queue so the ledger hooks degrade to a logged no-op there,
    # preserving the Postgres-free agent boundary (T-45-02).
    if ledger_sessionmaker is not None:
        q.ledger_sessionmaker = ledger_sessionmaker  # type: ignore[attr-defined]
    # Pool exhaustion (PoolTimeout) is the identified operational risk (36-RESEARCH Pitfall 4),
    # so surface the sizing decision at construction time.
    logger.debug("pipeline_queue_constructed", name=name, min_size=min_size, max_size=max_size, broker="postgres")
    return q


__all__ = ["build_pipeline_queue"]
