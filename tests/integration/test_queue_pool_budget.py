"""Real SAQ connect/count/disconnect honors the pool budget before opening connections."""

from __future__ import annotations

import asyncio
import os
import uuid

from psycopg import AsyncConnection

from phaze.tasks._shared.queue_factory import build_pipeline_queue
from tests.db_guard import libpq_dsn, require_test_database, resolve_test_dsn


async def test_real_queue_pools_never_open_the_upstream_default_four_connections() -> None:
    """Multiple lane queues must each respect max=2 immediately, not after a 600s shrink."""
    dsn = resolve_test_dsn()
    require_test_database(dsn, context="queue pool budget integration test")
    suffix = uuid.uuid4().hex
    application_name = f"pool-budget-{suffix}"
    queues = [
        build_pipeline_queue(f"pool-budget-{suffix}-{index}", libpq_dsn(dsn), cache_redis_url=os.environ["PHAZE_REDIS_URL"], min_size=0, max_size=2)
        for index in range(8)
    ]
    async with await AsyncConnection.connect(libpq_dsn(dsn), autocommit=True, application_name=f"budget-observer-{suffix}") as observer:

        async def actual_connections() -> int:
            cursor = await observer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = %s",
                (application_name,),
            )
            return (await cursor.fetchone())[0]

        try:
            for queue in queues:
                assert queue.pool.closed
                queue.pool.kwargs["application_name"] = application_name
                await queue.connect()
                assert await queue.count("queued") == 0
                assert await queue.count("active") == 0
                stats = queue.pool.get_stats()
                assert stats["pool_min"] == 0
                assert stats["pool_max"] == 2
                assert 0 < stats["pool_size"] <= 2
            assert sum(queue.pool.get_stats()["pool_size"] for queue in queues) <= 16
            assert 0 < await actual_connections() <= 16
        finally:
            for queue in queues:
                await queue.disconnect()
                await queue.cache_redis.aclose()
        assert all(queue.pool.closed and not queue._connected for queue in queues)
        async with asyncio.timeout(2):
            while await actual_connections():
                await asyncio.sleep(0.01)
