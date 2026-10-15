"""Real SAQ connect/count/disconnect honors the pool budget before opening connections."""

from __future__ import annotations

import uuid

from phaze.tasks._shared.queue_factory import build_pipeline_queue
from tests.db_guard import libpq_dsn, require_test_database, resolve_test_dsn


async def test_real_queue_pools_never_open_the_upstream_default_four_connections() -> None:
    """Multiple lane queues must each respect max=2 immediately, not after a 600s shrink."""
    import os

    dsn = resolve_test_dsn()
    require_test_database(dsn, context="queue pool budget integration test")
    suffix = uuid.uuid4().hex
    queues = [
        build_pipeline_queue(f"pool-budget-{suffix}-{index}", libpq_dsn(dsn), cache_redis_url=os.environ["PHAZE_REDIS_URL"], min_size=0, max_size=2)
        for index in range(8)
    ]
    try:
        for queue in queues:
            assert queue.pool.closed
            await queue.connect()
            assert await queue.count("queued") == 0
            assert await queue.count("active") == 0
            stats = queue.pool.get_stats()
            assert stats["pool_min"] == 0
            assert stats["pool_max"] == 2
            assert 0 < stats["pool_size"] <= 2
        assert sum(queue.pool.get_stats()["pool_size"] for queue in queues) <= 16
    finally:
        for queue in queues:
            await queue.disconnect()
            await queue.cache_redis.aclose()
    assert all(queue.pool.closed and not queue._connected for queue in queues)
