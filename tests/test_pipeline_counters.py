"""Unit tests for the maintained Redis pipeline counters (Phase 35 Plan 01).

Covers ``incr_enqueued`` / ``incr_completed`` (durable INCR, correct namespaced key)
and ``read_counters`` (merged ``{function: {enqueued, completed}}`` over the 8 known
functions, with misses reading back 0). Uses the in-memory :class:`FakeRedis` double.
"""

from __future__ import annotations

from phaze.services.pipeline_counters import (
    PIPELINE_FUNCTIONS,
    incr_completed,
    incr_enqueued,
    read_counters,
)
from tests._queue_fakes import FakeRedis


async def test_incr_enqueued_bumps_namespaced_key() -> None:
    redis = FakeRedis()
    await incr_enqueued(redis, "process_file")
    await incr_enqueued(redis, "process_file")
    assert redis.store["phaze:pipeline:enqueued:process_file"] == 2
    assert "phaze:pipeline:completed:process_file" not in redis.store


async def test_incr_completed_bumps_namespaced_key() -> None:
    redis = FakeRedis()
    await incr_completed(redis, "fingerprint_file")
    assert redis.store["phaze:pipeline:completed:fingerprint_file"] == 1
    assert "phaze:pipeline:enqueued:fingerprint_file" not in redis.store


async def test_read_counters_returns_merged_dict_for_all_functions() -> None:
    redis = FakeRedis()
    await incr_enqueued(redis, "process_file")
    await incr_enqueued(redis, "process_file")
    await incr_enqueued(redis, "process_file")
    await incr_completed(redis, "process_file")

    counters = await read_counters(redis)

    # Every known function is present in the merged result.
    assert set(counters) == set(PIPELINE_FUNCTIONS)
    # Seeded function reflects the exact INCR counts.
    assert counters["process_file"] == {"enqueued": 3, "completed": 1}
    # A function with no writes reads back zeros (the bytes/None -> 0 coercion path).
    assert counters["generate_proposals"] == {"enqueued": 0, "completed": 0}


async def test_read_counters_covers_eight_functions() -> None:
    redis = FakeRedis()
    counters = await read_counters(redis)
    assert len(counters) == 8
    for fn in PIPELINE_FUNCTIONS:
        assert counters[fn] == {"enqueued": 0, "completed": 0}
