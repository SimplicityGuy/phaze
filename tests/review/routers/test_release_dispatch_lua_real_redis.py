"""``_RELEASE_DISPATCH_LUA`` executed against REAL Redis (phaze-fx35j, seam E2).

WHY THIS FILE EXISTS, AND WHY ``test_execution_helpers.py`` DOES NOT COVER IT
--------------------------------------------------------------------------------
``tests/review/routers/test_execution_helpers.py``'s ``dispatch_app`` fixture builds a
``MagicMock`` Redis client and monkeypatches ``_get_release_dispatch_script`` itself to return an
``AsyncMock``. That proves the CALLER (``execution.py``) invokes the release helper with the right
``keys``/``args`` shape at the right point in the dispatch-failure path; it never runs the actual
``_RELEASE_DISPATCH_LUA`` text through a Lua interpreter. The compare-and-delete guard --
``GET(KEYS[1]) == ARGV[1]`` before the ``DEL`` -- is exactly the kind of logic a mock cannot
exercise: a mock has no notion of "the key currently holds someone else's value", so a script that
deleted unconditionally would pass that test file just as well as the real one.

This file imports ``_RELEASE_DISPATCH_LUA`` and ``_get_release_dispatch_script`` from production
and runs the real script, registered on a real client, against a real Redis server.

PRODUCTION CLIENT MODE
-----------------------
``_get_release_dispatch_script`` is always called with ``deps.redis_client``
(``services/execution_dispatch_protocol.py``), which is ``app.state.redis`` --
``redis_async.Redis.from_url(settings.redis_url, decode_responses=True)`` (``main.py:112``). The
``str_mode_redis`` fixture below matches that exactly. Since ``KEYS[1]``/``ARGV[1]`` are compared
with Lua string equality and the sentinel value is itself a UUID string written by the SAME
str-mode client in production, the byte-vs-str client-mode boundary (seam E3/E4, phaze-ooe68) is
not this script's residual risk -- its residual risk is the CAS logic itself, which is what these
tests target.

REDIS VERSION
--------------
Run against the harness's ``redis:7-alpine`` (measured ``redis_version:7.4.11`` via
``docker exec phaze-test-redis redis-cli INFO server``). Production runs ``redis:8-alpine``
(``docker-compose.yml`` / CLAUDE.md's "Known gap"). This script's commands -- ``GET``/``DEL`` --
have identical reply semantics on both lines; Redis 8 only adds commands over 7.x, it does not
change these.

CONCURRENCY / THE "DOUBLE-MOVE" HAZARD
----------------------------------------
The script's own comment names the failure it exists to prevent: "an unconditional DEL would drop
a NEWER dispatch's claim, re-opening the double-move hazard the sentinel exists to close."
``test_release_never_drops_a_newer_dispatchs_claim`` and
``test_n_concurrent_releases_for_the_same_owner_let_exactly_one_delete`` put exactly that scenario
in front of a real server: a late release racing (or arriving after) a newer claim on the same
sentinel key must never delete the newer claim, and of N concurrent releases claiming the SAME
owner (the retry-after-timeout shape), exactly one may actually perform the delete.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from phaze.routers.agent_exec_batches import _get_release_dispatch_script


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from redis.commands.core import AsyncScript


# Real Redis is mandatory here; these tests are about a real Lua interpreter's CAS behaviour.
pytestmark = pytest.mark.integration

_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")


@pytest_asyncio.fixture
async def str_mode_redis() -> AsyncGenerator[redis_async.Redis]:
    """A client wired EXACTLY as ``app.state.redis`` is (``main.py:112``): ``decode_responses=True``.

    This is production's ONLY caller of ``_get_release_dispatch_script`` -- see the module docstring.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def sentinel_key() -> str:
    """A per-test sentinel key, so no two tests (or seats) can ever collide on ``execdispatch:active``."""
    return f"execdispatch:active:test:{uuid.uuid4()}"


@pytest_asyncio.fixture(autouse=True)
async def _drop_sentinel_key(str_mode_redis: redis_async.Redis, sentinel_key: str) -> AsyncGenerator[None]:
    yield
    await str_mode_redis.delete(sentinel_key)


@pytest.fixture
def release_script(str_mode_redis: redis_async.Redis) -> AsyncScript:
    """The real, production ``register_script``-backed release script, bound to the real client."""
    return _get_release_dispatch_script(str_mode_redis)


async def test_release_by_owner_deletes_the_sentinel(
    release_script: AsyncScript,
    str_mode_redis: redis_async.Redis,
    sentinel_key: str,
) -> None:
    """The straightforward path: the caller supplying the batch_id that currently owns the key wins."""
    batch_id = str(uuid.uuid4())
    await str_mode_redis.set(sentinel_key, batch_id, ex=60)

    result = await release_script(keys=[sentinel_key], args=[batch_id], client=str_mode_redis)

    assert int(result) == 1
    assert await str_mode_redis.get(sentinel_key) is None


async def test_release_by_non_owner_is_a_noop_and_leaves_the_real_owners_claim_intact(
    release_script: AsyncScript,
    str_mode_redis: redis_async.Redis,
    sentinel_key: str,
) -> None:
    """A release carrying someone ELSE's batch_id must not touch a key it does not own.

    This is the mutation target named in the bead: "a release that deletes someone else's token".
    A script that dropped the ``GET`` guard (or inverted it) would delete here.
    """
    real_owner = str(uuid.uuid4())
    impostor = str(uuid.uuid4())
    await str_mode_redis.set(sentinel_key, real_owner, ex=60)

    result = await release_script(keys=[sentinel_key], args=[impostor], client=str_mode_redis)

    assert int(result) == 0
    assert await str_mode_redis.get(sentinel_key) == real_owner


async def test_release_when_the_key_is_missing_is_a_noop(
    release_script: AsyncScript,
    str_mode_redis: redis_async.Redis,
    sentinel_key: str,
) -> None:
    """Key-missing edge (named explicitly in the bead's acceptance): GET returns nil, no error, no DEL."""
    assert await str_mode_redis.get(sentinel_key) is None

    result = await release_script(keys=[sentinel_key], args=[str(uuid.uuid4())], client=str_mode_redis)

    assert int(result) == 0
    assert await str_mode_redis.get(sentinel_key) is None


async def test_release_never_drops_a_newer_dispatchs_claim(
    release_script: AsyncScript,
    str_mode_redis: redis_async.Redis,
    sentinel_key: str,
) -> None:
    """The exact double-move hazard the script's own comment names.

    Dispatch A claims the sentinel, its promotion path never runs (crash / all-enqueues-failed), and
    its release call is delayed. Meanwhile the sentinel's safety TTL lapses (or its hash was reaped
    and reconciled) and dispatch B legitimately claims the SAME key. A's late release must not be
    able to delete B's live claim just because it targets the same key -- only A's own batch_id
    would have matched, and it no longer does.
    """
    stale_batch_a = str(uuid.uuid4())
    newer_batch_b = str(uuid.uuid4())
    await str_mode_redis.set(sentinel_key, newer_batch_b, ex=60)  # B now holds it

    result = await release_script(keys=[sentinel_key], args=[stale_batch_a], client=str_mode_redis)

    assert int(result) == 0
    assert await str_mode_redis.get(sentinel_key) == newer_batch_b, "B's live claim must survive A's stale release"


async def test_n_concurrent_releases_for_the_same_owner_let_exactly_one_delete(
    release_script: AsyncScript,
    str_mode_redis: redis_async.Redis,
    sentinel_key: str,
) -> None:
    """N real concurrent release attempts, same correct owner: exactly one performs the DEL.

    Models the retry-after-timeout shape noted in the source comment (``phaze-tnp06``): more than
    one caller can end up racing to release the same claim. Only the first to actually execute
    server-side finds the key still present; every other concurrent attempt hits the (by-then)
    key-missing edge and returns 0 -- proving the CAS is safe under real concurrent pressure, not
    just when called once.
    """
    n = 8
    owner = str(uuid.uuid4())
    await str_mode_redis.set(sentinel_key, owner, ex=60)

    results = await asyncio.gather(*(release_script(keys=[sentinel_key], args=[owner], client=str_mode_redis) for _ in range(n)))

    successes = [r for r in results if int(r) == 1]
    noops = [r for r in results if int(r) == 0]
    assert len(successes) == 1, f"exactly one concurrent release must win the DEL, got {len(successes)} of {n}"
    assert len(noops) == n - 1
    assert await str_mode_redis.get(sentinel_key) is None


async def test_registering_the_script_twice_reuses_the_cached_asyncscript(
    str_mode_redis: redis_async.Redis,
    sentinel_key: str,
) -> None:
    """Sanity on the production getter's caching: two calls return the SAME object and both still work.

    ``_get_release_dispatch_script`` caches at module scope after first registration
    (``agent_exec_batches.py``). This guards against a future change accidentally re-registering
    (or worse, rebinding to a stale client) on every call.
    """
    first = _get_release_dispatch_script(str_mode_redis)
    second = _get_release_dispatch_script(str_mode_redis)
    assert first is second

    batch_id = str(uuid.uuid4())
    await str_mode_redis.set(sentinel_key, batch_id, ex=60)
    result = await second(keys=[sentinel_key], args=[batch_id], client=str_mode_redis)
    assert int(result) == 1
