"""The pipeline counters' bytes-vs-str Redis client-mode boundary, crossed against REAL Redis (phaze-ooe68, seam E4).

WHY THIS FILE EXISTS, AND WHY THE EXISTING TESTS DO NOT COVER IT
----------------------------------------------------------------
``tests/analyze/core/test_pipeline_counters.py`` drives the counters through
:class:`tests._queue_fakes.FakeRedis`, an in-memory ``dict[str, int]``. A dict has no client
mode at all: it returns whatever was put in it, so the writer and the reader can never disagree
about bytes-vs-str, and the boundary this module tests is structurally absent from that harness.
That is the ADR-0012 rule 3 shape -- a claim about a real client's decoding is not discharged by
a double that does no decoding -- and it is what the bead calls the E4 "proxy: FakeRedis dict".

THE PRODUCTION PAIR THIS FILE REPRODUCES (measured on the bead, 2026-08-24)
--------------------------------------------------------------------------
Writers -- ``queue.cache_redis``, built ``aioredis.Redis.from_url(cache_redis_url)`` with NO
``decode_responses`` (``tasks/_shared/queue_factory.py:95``), so a BYTE-mode client:

* :func:`phaze.services.pipeline_counters.incr_enqueued`, from the ``before_enqueue`` hook
  ``apply_deterministic_key`` (``tasks/_shared/deterministic_key.py``);
* :func:`phaze.services.pipeline_counters.incr_completed`, from ``_bump_completed_counter`` on
  the ``after_process`` hook (same module).

Reader -- ``app.state.redis``, built ``Redis.from_url(settings.redis_url, decode_responses=True)``
(``main.py:161``), so a STR-mode client, reached through
:func:`phaze.routers.pipeline.dashboard_stats._read_pipeline_counters` ->
:func:`phaze.services.pipeline_counters.read_counters` -> ``MGET`` -> ``_to_int`` -> the dashboard.

The two sides therefore do NOT share a client mode in production, and every test below keeps them
apart. Both clients are opened on the SAME real Redis logical database, because a cross-mode
round trip is the whole point -- one seat's ``PHAZE_REDIS_URL``, per CLAUDE.md "Why Redis matters
as much as Postgres".

WHAT THE CROSSING ACTUALLY PROVES
---------------------------------
``INCR`` is mode-independent on the wire: client mode changes what a READ decodes to and nothing
else. So the production pair (byte writer -> str reader) works, and that is worth pinning rather
than assuming -- it is the reason a byte-mode writer is NOT an argument for tolerating byte-mode
reads, which is exactly the argument ``_to_int``'s old bimodality rested on.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from phaze.routers.pipeline.dashboard_stats import _read_pipeline_counters
from phaze.services.pipeline_counters import (
    PIPELINE_FUNCTIONS,
    _completed_key,
    _enqueued_key,
    incr_completed,
    incr_enqueued,
    read_counters,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# Real Redis is mandatory here; these tests are about a real client's decoding.
pytestmark = pytest.mark.integration

_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")

# Every key this module can touch, named explicitly. Deliberately NOT a ``scan_iter`` sweep over
# ``phaze:pipeline:*``: CLAUDE.md records that global sweeps in redis-backed test fixtures are how
# one seat deletes another's live keys, and the counter key space is small and fully enumerable.
_ALL_COUNTER_KEYS = [_enqueued_key(fn) for fn in PIPELINE_FUNCTIONS] + [_completed_key(fn) for fn in PIPELINE_FUNCTIONS]


@pytest_asyncio.fixture
async def byte_mode_redis() -> AsyncGenerator[redis_async.Redis]:
    """A client wired EXACTLY as ``queue.cache_redis`` is (``queue_factory.py:95``): no ``decode_responses``.

    This is production's WRITER for these keys. It also clears the counter keys either side of the
    test so a rerun starts from a known zero -- the counters are durable, never-reset ``INCR``s.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL)
    await client.delete(*_ALL_COUNTER_KEYS)
    try:
        yield client
    finally:
        await client.delete(*_ALL_COUNTER_KEYS)
        await client.aclose()


@pytest_asyncio.fixture
async def str_mode_redis() -> AsyncGenerator[redis_async.Redis]:
    """A client wired EXACTLY as ``app.state.redis`` is (``main.py:161``): ``decode_responses=True``.

    This is production's READER for these keys -- the handle the dashboard poll passes down.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


async def test_byte_mode_writer_and_str_mode_reader_agree_on_real_redis(
    byte_mode_redis: redis_async.Redis,
    str_mode_redis: redis_async.Redis,
) -> None:
    """The production pair, crossed: ``incr_*`` through the byte-mode client, ``read_counters`` through the str-mode one.

    Neither side is the other's client, and neither is a fake. This is the assertion the FakeRedis
    suite structurally cannot make.
    """
    for _ in range(3):
        await incr_enqueued(byte_mode_redis, "process_file")
    await incr_completed(byte_mode_redis, "process_file")
    await incr_enqueued(byte_mode_redis, "generate_proposals")

    counters = await read_counters(str_mode_redis)

    assert set(counters) == set(PIPELINE_FUNCTIONS)
    assert counters["process_file"] == {"enqueued": 3, "completed": 1}
    assert counters["generate_proposals"] == {"enqueued": 1, "completed": 0}
    # A function nobody wrote reads back as a genuine MGET miss (None), not as a decode artefact.
    assert counters["push_file"] == {"enqueued": 0, "completed": 0}


async def test_the_two_clients_really_do_disagree_about_the_same_key(
    byte_mode_redis: redis_async.Redis,
    str_mode_redis: redis_async.Redis,
) -> None:
    """Guard on the harness itself: prove the fixtures are genuinely two DIFFERENT client modes.

    Without this, every test in this file could silently degrade into the defect it was written to
    close -- two clients that happen to share a mode, which is precisely what the existing
    ``exec:`` round-trip test does. Asserting the raw ``GET`` types keeps the crossing honest.
    """
    await incr_enqueued(byte_mode_redis, "push_file")
    key = _enqueued_key("push_file")

    assert await byte_mode_redis.get(key) == b"1"
    assert await str_mode_redis.get(key) == "1"


async def test_read_counters_refuses_a_byte_mode_reader_against_real_redis(
    byte_mode_redis: redis_async.Redis,
) -> None:
    """Reading the counters with the WRITER's client mode is now a loud ``TypeError``, not a silent success.

    This is the real-Redis half of the ``_to_int`` pin the bead requires: the ``bytes`` here are
    produced by an actual byte-mode redis-py client decoding an actual ``MGET`` reply, not by a
    ``b"7"`` literal in a unit test. Before phaze-ooe68 this call returned the right numbers and
    said nothing, which is what made the client-mode mismatch unobservable everywhere it occurred.
    """
    await incr_enqueued(byte_mode_redis, "process_file")

    with pytest.raises(TypeError, match="requires the decode_responses=True client"):
        await read_counters(byte_mode_redis)


async def test_the_dashboard_reader_degrades_rather_than_500s_on_a_byte_mode_client(
    byte_mode_redis: redis_async.Redis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The REAL consumer, handed the wrong client mode, degrades to DB-truth and logs (ADR-0012 rule 3).

    ``_to_int``'s strictness is only defensible because of what its one caller does with the
    raise, so the caller is what this test drives -- not ``read_counters`` in isolation. T-35-09
    says the 5s dashboard poll must never 500; D-03 says the DB owns every rendered ``done`` and
    these counters are a cache. Both still hold with a strict ``_to_int``: the mismatch surfaces
    as ``pipeline_counters_degraded`` with a traceback and an empty counter dict.
    """

    class _AppState:
        """The two attributes ``_read_pipeline_counters`` reaches for on ``app.state``."""

        redis: Any = byte_mode_redis

    await incr_enqueued(byte_mode_redis, "process_file")

    with caplog.at_level("WARNING"):
        counters = await _read_pipeline_counters(_AppState())

    assert counters == {}
    assert "pipeline_counters_degraded" in caplog.text
