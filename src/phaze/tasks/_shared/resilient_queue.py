"""Postgres-backed SAQ queue with bounded upkeep recovery.

SAQ's worker already keeps its upkeep poller alive after an exception, but its default sweep
interval makes a single transient connection-acquisition failure skip the entire interval.  This
queue retries that narrow failure class within the current tick while leaving SAQ's sweep state
machine untouched.
"""

from __future__ import annotations

import asyncio
import secrets

import psycopg
from psycopg_pool import PoolTimeout
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
