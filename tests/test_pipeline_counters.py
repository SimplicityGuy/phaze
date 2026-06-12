"""Unit tests for the maintained Redis pipeline counters (Phase 35 Plan 01).

Covers ``incr_enqueued`` / ``incr_completed`` (durable INCR, correct namespaced key)
and ``read_counters`` (merged ``{function: {enqueued, completed}}`` over the 8 known
functions, with misses reading back 0). Uses the in-memory :class:`FakeRedis` double.
"""

from __future__ import annotations

from phaze.services.pipeline_counters import (
    PIPELINE_FUNCTIONS,
    _to_int,
    incr_completed,
    incr_enqueued,
    read_counters,
)
from tests._queue_fakes import FakeRedis


def test_to_int_coerces_none_bytes_str_and_int() -> None:
    """``_to_int`` handles every Redis return shape: a miss (None), bytes (non-decode_responses
    client), and a plain str/int (decode_responses=True client)."""
    assert _to_int(None) == 0
    assert _to_int(b"7") == 7
    assert _to_int("42") == 42  # decode_responses=True path
    assert _to_int(13) == 13  # already-int path


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
