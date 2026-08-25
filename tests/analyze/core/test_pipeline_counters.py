"""Unit tests for the maintained Redis pipeline counters (Phase 35 Plan 01).

Covers ``incr_enqueued`` / ``incr_completed`` (durable INCR, correct namespaced key)
and ``read_counters`` (merged ``{function: {enqueued, completed}}`` over the 9 known
functions, with misses reading back 0). Uses the in-memory :class:`FakeRedis` double.
"""

from __future__ import annotations

import pytest

from phaze.services.pipeline_counters import (
    PIPELINE_FUNCTIONS,
    _to_int,
    incr_completed,
    incr_enqueued,
    read_counters,
)
from tests._queue_fakes import FakeRedis


def test_to_int_accepts_the_decode_responses_shapes_and_refuses_bytes() -> None:
    """``_to_int`` accepts what the ONE production reader yields, and refuses what it never yields.

    phaze-ooe68 (seam E4): ``_to_int`` used to decode ``bytes`` as well, which made a Redis
    client-mode mismatch succeed silently. It is now STRICT -- an implementer's decision argued
    in the helper's own docstring. This is the unit half of the pin required by the bead; the
    real-Redis half, where the ``bytes`` come from an actual byte-mode client rather than a
    hand-written literal, is ``tests/analyze/core/test_pipeline_counters_client_mode.py``.
    """
    assert _to_int(None) == 0  # a miss
    assert _to_int("42") == 42  # decode_responses=True path -- app.state.redis (main.py:161)
    assert _to_int(13) == 13  # already-int path

    # The masking branch, now refused. Both spellings, since redis-py can hand back either.
    for wrong in (b"7", bytearray(b"7")):
        with pytest.raises(TypeError, match="requires the decode_responses=True client"):
            _to_int(wrong)


async def test_incr_enqueued_bumps_namespaced_key() -> None:
    redis = FakeRedis()
    await incr_enqueued(redis, "process_file")
    await incr_enqueued(redis, "process_file")
    assert redis.store["phaze:pipeline:enqueued:process_file"] == 2
    assert "phaze:pipeline:completed:process_file" not in redis.store


async def test_incr_completed_bumps_namespaced_key() -> None:
    redis = FakeRedis()
    await incr_completed(redis, "extract_file_metadata")
    assert redis.store["phaze:pipeline:completed:extract_file_metadata"] == 1
    assert "phaze:pipeline:enqueued:extract_file_metadata" not in redis.store


async def test_read_counters_returns_merged_dict_for_all_functions() -> None:
    # phaze-ooe68: ``read_counters`` has exactly one production reader and it passes
    # ``app.state.redis`` (decode_responses=True), so the double must stand in for THAT mode.
    # The default byte-mode FakeRedis is the ``queue.cache_redis`` WRITER handle.
    redis = FakeRedis(decode_responses=True)
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


async def test_read_counters_covers_all_functions() -> None:
    redis = FakeRedis(decode_responses=True)  # the dashboard reader's mode -- see above
    counters = await read_counters(redis)
    assert len(counters) == len(PIPELINE_FUNCTIONS) == 5
    for fn in PIPELINE_FUNCTIONS:
        assert counters[fn] == {"enqueued": 0, "completed": 0}
