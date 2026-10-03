"""Postgres-backed SAQ queue with bounded upkeep recovery.

SAQ's worker already keeps its upkeep poller alive after an exception, but its default sweep
interval makes a single transient connection-acquisition failure skip the entire interval.  This
queue retries that narrow failure class within the current tick while leaving SAQ's sweep state
machine untouched.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import secrets
from typing import Any

import psycopg
from psycopg_pool import PoolTimeout
from saq import Job
from saq.job import TERMINAL_STATUSES
from saq.queue.postgres import PostgresQueue
import structlog


logger = structlog.get_logger(__name__)

UPKEEP_RETRY_ATTEMPTS = 3
UPKEEP_RETRY_BASE_DELAY_SECONDS = 1.0
_UPKEEP_METRIC_NAMESPACE = "phaze:metrics:saq_upkeep"
_RETRYABLE_UPKEEP_EXCEPTIONS = (PoolTimeout, psycopg.OperationalError)
_RANDOM = secrets.SystemRandom()


class ResilientPostgresQueue(PostgresQueue):
    """Retry transient SAQ sweep connection failures without hiding persistent outages."""

    async def enqueue(self, job_or_func: str | Job, **kwargs: Any) -> Job | None:
        """Serialize control-side analysis attempts across the ledger and broker commits."""
        function = job_or_func.function if isinstance(job_or_func, Job) else job_or_func
        # Match SAQ's task-kwargs normalization before choosing the serialization key.
        # Job field overrides are separate from task arguments; supplied task arguments
        # replace a Job instance's kwargs just as Queue.enqueue does.
        options: dict[str, Any] = {}
        for name, value in kwargs.items():
            if name in Job.__dataclass_fields__:
                options[name] = value
            else:
                options.setdefault("kwargs", {})[name] = value
        payload = options.get("kwargs", job_or_func.kwargs if isinstance(job_or_func, Job) else {})
        if isinstance(job_or_func, Job):
            function = options.get("function", function)
        maker = getattr(self, "ledger_sessionmaker", None)
        if function != "process_file" or maker is None or not isinstance(payload, dict) or "file_id" not in payload:
            return await super().enqueue(job_or_func, **kwargs)
        registered_queue = options.get("queue", job_or_func.queue if isinstance(job_or_func, Job) else None)
        if registered_queue is not None and registered_queue.name != self.name:
            raise ValueError(f"Job registered to a different queue: {registered_queue.name}")
        # Control-only lazy import: agent queues have no ledger handle.
        from phaze.services.analysis_attempt import serialize_analysis_enqueue  # noqa: PLC0415
        from phaze.tasks._shared.attempt_context import ledger_enqueue_session  # noqa: PLC0415

        key = f"process_file:{(payload or {}).get('file_id')}"
        async with AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(serialize_analysis_enqueue(maker, key))
            except Exception:
                # Preserve T-45-03: ledger bookkeeping must never stop the broker enqueue.
                # This degraded attempt has no trustworthy token; startup backfill repairs
                # the obligation as before. Only acquisition is caught, never broker errors.
                logger.warning("analysis_attempt_lock_degraded", key=key, exc_info=True)
            if ledger_enqueue_session.get() is not None:
                live = await self.job(key)
                if live is not None and live.status not in TERMINAL_STATUSES:
                    # Linearize dedup at this read. A finish after the read cannot turn
                    # this duplicate producer into a new accepted attempt with an old token.
                    return None
            return await super().enqueue(job_or_func, **kwargs)

    async def _increment_upkeep_metric(self, metric: str) -> None:
        """Best-effort durable metric; observability must not replace the original failure."""
        cache_redis = getattr(self, "cache_redis", None)
        if cache_redis is None:
            return
        try:
            await cache_redis.incr(f"{_UPKEEP_METRIC_NAMESPACE}:{metric}:{self.name}")
        except Exception:
            logger.warning("saq_upkeep_metric_write_failed", queue=self.name, metric=metric, exc_info=True)

    async def sweep(self, lock: int = 60, abort: float = 5.0) -> list[str]:
        """Run SAQ's unmodified sweep with bounded exponential backoff and jitter."""
        for attempt in range(1, UPKEEP_RETRY_ATTEMPTS + 1):
            try:
                return await super().sweep(lock=lock, abort=abort)
            except _RETRYABLE_UPKEEP_EXCEPTIONS as exc:
                if attempt == UPKEEP_RETRY_ATTEMPTS:
                    await self._increment_upkeep_metric("failures_total")
                    logger.error(
                        "saq_upkeep_retry_exhausted",
                        queue=self.name,
                        attempt=attempt,
                        attempts=UPKEEP_RETRY_ATTEMPTS,
                        error_type=type(exc).__name__,
                        exc_info=True,
                    )
                    raise

                await self._increment_upkeep_metric("retries_total")
                delay = UPKEEP_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)) * _RANDOM.uniform(0.8, 1.2)
                logger.warning(
                    "saq_upkeep_retrying",
                    queue=self.name,
                    attempt=attempt,
                    attempts=UPKEEP_RETRY_ATTEMPTS,
                    delay_seconds=delay,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(delay)

        raise AssertionError("unreachable")


def upkeep_metric_key(metric: str, queue_name: str) -> str:
    """Return the Redis key exported for an upkeep counter."""
    return f"{_UPKEEP_METRIC_NAMESPACE}:{metric}:{queue_name}"


__all__ = ["ResilientPostgresQueue", "upkeep_metric_key"]
