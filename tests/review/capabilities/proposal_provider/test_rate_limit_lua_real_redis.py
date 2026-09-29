"""``_RATE_LIMIT_LUA`` executed against REAL Redis (phaze-fx35j, seam E2).

WHY THIS FILE EXISTS, AND WHY ``test_service.py`` DOES NOT COVER IT
---------------------------------------------------------------------
``tests/review/capabilities/proposal_provider/test_service.py`` exercises ``check_rate_limit``
against ``_FakeRedis``, an in-memory Python class whose ``eval()`` RE-IMPLEMENTS the Lua script's
semantics in Python (INCR, then arm the TTL if it reads -1). That proxy can only ever agree with
its own author's model of the script -- it never asks a real Lua interpreter to run the actual
``_RATE_LIMIT_LUA`` text, so a real bug in the script text (wrong ``KEYS``/``ARGV`` index, a
``tonumber`` coercion mistake, an off-by-one against the TTL sentinel) is invisible to it. Every
test below imports ``_RATE_LIMIT_LUA`` from production and runs it through ``redis_pool.eval``
against a live Redis server -- the same call ``check_rate_limit`` makes.

PRODUCTION CLIENT MODE
-----------------------
``check_rate_limit``'s ``redis_pool`` argument is ``ctx["redis"]``, a per-worker cache handle
stashed by the control worker's ``startup`` (``tasks/controller.py:250`` /
``tasks/agent_worker.py:337``): ``redis_async.Redis.from_url(cfg.redis_url)`` with NO
``decode_responses`` kwarg -- i.e. the redis-py default, byte mode. This is a DIFFERENT client mode
from ``app.state.redis`` (``main.py:112``, ``decode_responses=True``) used elsewhere in the app.
``byte_mode_redis`` below is wired to match. It does not matter for THIS script's return value
(``EVAL`` returns a Lua number, and redis-py converts a Lua integer reply to a Python ``int``
regardless of ``decode_responses`` -- that flag only affects bulk-string replies), but the fixture
is wired to match production anyway so the client-mode fact stated in the code stays load-bearing
here too, per CLAUDE.md's "verify with the artifact's real consumer" rule.

REDIS VERSION
--------------
Written against the harness's former ``redis:7-alpine`` (``redis_version:7.4.11``) while production
ran ``redis:8-alpine``. Since phaze-8o294 the harness, CI and production all run the same
``valkey/valkey:9-alpine`` (measured ``valkey_version:9.1.2`` via ``redis-cli INFO server``), so the
skew this section used to flag is closed rather than argued: this script was run through redis-py on
redis 7.4.11, redis 8.10.2 and Valkey 9.1.2 with identical replies and key state (bead
``phaze-8o294``'s comments carry the differential).

CONCURRENCY
------------
``test_n_concurrent_callers_against_limit_k_let_exactly_k_through`` is the reason this has to be a
REAL server: the atomicity the script exists for (one INCR, one TTL-arm decision, indivisible) is
exactly the property an in-process Python fake cannot put under real pressure, because a Python
coroutine fake never actually races -- it just runs its own statements in whatever order asyncio
schedules them, cooperatively, inside one interpreter. A real Redis server serializes concurrent
``EVAL`` calls from multiple real network connections, which is the actual guarantee the production
rate limiter depends on.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from phaze.services.proposal_provider import _RATE_LIMIT_LUA, _RATE_LIMIT_WINDOW_SEC, check_rate_limit


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# Real Redis is mandatory here; these tests are about a real Lua interpreter's behaviour.
pytestmark = pytest.mark.integration

_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")

# The literal key check_rate_limit hard-codes (services/proposal_provider.py). Not
# parametrizable from the call site, so tests that exercise check_rate_limit ITSELF (as opposed to
# raw-eval'ing the script text against a throwaway key) share this key and rely on the autouse
# fixture below to reset it around each test rather than a fresh uuid per test.
_PROD_RATE_LIMIT_KEY = "phaze:llm:rpm"


@pytest_asyncio.fixture
async def byte_mode_redis() -> AsyncGenerator[redis_async.Redis]:
    """Wired exactly as ``ctx["redis"]`` is in production: no ``decode_responses``.

    See the module docstring -- this is the byte-mode cache handle the control worker hands
    ``check_rate_limit``, distinct from ``app.state.redis``'s ``decode_responses=True``.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_prod_rate_limit_key(byte_mode_redis: redis_async.Redis) -> AsyncGenerator[None]:
    """Reset the fixed production key around every test that drives ``check_rate_limit`` directly.

    Deliberately a single named key, not a ``scan_iter`` sweep (CLAUDE.md records global sweeps in
    redis-backed fixtures as the mechanism by which one seat destroys another's live keys) -- this
    worktree's Redis logical DB is this seat's alone, so a fixed key is safe within it.
    """
    await byte_mode_redis.delete(_PROD_RATE_LIMIT_KEY)
    yield
    await byte_mode_redis.delete(_PROD_RATE_LIMIT_KEY)


@pytest.fixture
def scratch_key() -> str:
    """A per-test key for tests that raw-eval the script text without going through ``check_rate_limit``."""
    return f"phaze:llm:rpm:test:{uuid.uuid4()}"


async def _server_ms(redis: redis_async.Redis) -> int:
    """Redis's own clock, in ms: the clock the key's expiry is kept on."""
    seconds, micros = await redis.time()
    return int(seconds) * 1000 + int(micros) // 1000


async def _assert_armed_for(redis: redis_async.Redis, key: str, seconds: int, *, between: tuple[int, int]) -> None:
    """``key``'s expiry was set to exactly ``seconds`` at some instant inside ``between`` (server ms).

    Not ``TTL == seconds`` (phaze-0vlnp). ``TTL`` reports what is LEFT, rounded to the second, so it
    read 59 against a 60 s window once 0.5 s of a loaded machine passed between the arming EVAL and
    the read -- a real-Redis gate went red on exactly that. The absolute expiry (``PEXPIRETIME``) and
    the bracketing ``TIME`` reads are all on the server's clock, so the check is exact however long
    the round trips take, and a wrong window, a wrong unit or no expiry at all still fails it.
    """
    armed_after, armed_before = between
    expires_at = await redis.pexpiretime(key)
    assert armed_after + seconds * 1000 <= expires_at <= armed_before + seconds * 1000, (
        f"expected an expiry {seconds}s after an arming inside [{armed_after}, {armed_before}] ms, got {expires_at}"
    )


class _RateLimited(Exception):
    """Sentinel the test's ``sleep`` stub raises instead of really backing off 2s.

    Deliberately NOT ``asyncio.CancelledError`` -- raising that from inside a task's own body,
    rather than via external cancellation, is an easy way to make ``asyncio.gather`` behave in
    ways that are surprising and version-dependent. A plain exception class keeps the "this caller
    got refused" signal unambiguous.
    """


async def _refuse_and_stop(_delay: float) -> None:
    raise _RateLimited


async def test_first_call_on_a_fresh_key_arms_the_ttl(scratch_key: str, byte_mode_redis: redis_async.Redis) -> None:
    """Raw-eval the real script text on a fresh key: INCR to 1, TTL was -1, so it gets armed."""
    before = await _server_ms(byte_mode_redis)
    count = int(await byte_mode_redis.eval(_RATE_LIMIT_LUA, 1, scratch_key, _RATE_LIMIT_WINDOW_SEC))
    after = await _server_ms(byte_mode_redis)

    assert count == 1
    await _assert_armed_for(byte_mode_redis, scratch_key, _RATE_LIMIT_WINDOW_SEC, between=(before, after))


async def test_a_ttl_less_key_self_heals_on_the_next_eval(scratch_key: str, byte_mode_redis: redis_async.Redis) -> None:
    """Regression this script exists for (phaze-pkgb): a counter stuck without a TTL re-arms.

    Seed the key directly (bypassing the script) at count=5 with NO expiry -- the "lost EXPIRE"
    state a crash between INCR and EXPIRE could leave under the OLD non-atomic implementation.
    """
    await byte_mode_redis.set(scratch_key, 5)
    assert await byte_mode_redis.ttl(scratch_key) == -1

    before = await _server_ms(byte_mode_redis)
    count = int(await byte_mode_redis.eval(_RATE_LIMIT_LUA, 1, scratch_key, _RATE_LIMIT_WINDOW_SEC))
    after = await _server_ms(byte_mode_redis)

    assert count == 6
    await _assert_armed_for(byte_mode_redis, scratch_key, _RATE_LIMIT_WINDOW_SEC, between=(before, after))


async def test_an_already_armed_ttl_is_not_reset(scratch_key: str, byte_mode_redis: redis_async.Redis) -> None:
    """A key mid-window (TTL already armed, not -1) must NOT have its TTL clobbered back to the full window.

    This is the mutation this test is aimed at: an off-by-one or wrong-sentinel change to the
    ``== -1`` guard (e.g. arming unconditionally, or on ``<= 0``) would keep resetting a live
    window's countdown on every call and the rate limiter would never actually roll.
    """
    await byte_mode_redis.eval(_RATE_LIMIT_LUA, 1, scratch_key, _RATE_LIMIT_WINDOW_SEC)
    await byte_mode_redis.expire(scratch_key, 5)  # simulate time having passed within the window
    armed_at = await byte_mode_redis.pexpiretime(scratch_key)
    assert armed_at > 0

    await byte_mode_redis.eval(_RATE_LIMIT_LUA, 1, scratch_key, _RATE_LIMIT_WINDOW_SEC)

    # The absolute expiry, not the remaining TTL (phaze-0vlnp): unchanged to the millisecond, however long the EVAL took.
    assert await byte_mode_redis.pexpiretime(scratch_key) == armed_at, "an already-armed TTL must be left alone, not reset to the full window"


async def test_over_limit_path_decrements_then_recovers_when_the_window_lapses(byte_mode_redis: redis_async.Redis) -> None:
    """``check_rate_limit`` itself, real script + a real compensating DECR, then a real retry.

    Pre-seed the counter AT the limit (armed TTL, matching a live window). The first loop
    iteration's INCR pushes it over, so it must DECR back and back off; the injected ``sleep``
    models the window lapsing (key deleted, as Redis itself would do on TTL expiry) and the retry
    then succeeds against a real fresh key.
    """
    max_rpm = 5
    await byte_mode_redis.eval(_RATE_LIMIT_LUA, 1, _PROD_RATE_LIMIT_KEY, _RATE_LIMIT_WINDOW_SEC)
    for _ in range(max_rpm - 1):
        await byte_mode_redis.incr(_PROD_RATE_LIMIT_KEY)
    assert int(await byte_mode_redis.get(_PROD_RATE_LIMIT_KEY)) == max_rpm

    sleeps: list[float] = []

    async def _lapse_window(delay: float) -> None:
        sleeps.append(delay)
        await byte_mode_redis.delete(_PROD_RATE_LIMIT_KEY)

    before = await _server_ms(byte_mode_redis)
    await check_rate_limit(byte_mode_redis, max_rpm, sleep=_lapse_window)
    after = await _server_ms(byte_mode_redis)

    assert sleeps == [2.0]
    assert int(await byte_mode_redis.get(_PROD_RATE_LIMIT_KEY)) == 1
    await _assert_armed_for(byte_mode_redis, _PROD_RATE_LIMIT_KEY, _RATE_LIMIT_WINDOW_SEC, between=(before, after))


async def test_n_concurrent_callers_against_limit_k_let_exactly_k_through(byte_mode_redis: redis_async.Redis) -> None:
    """N real concurrent ``check_rate_limit`` callers, limit K: exactly K return, N-K are refused.

    Every refused caller's ``sleep`` stub raises immediately rather than really backing off, so
    this resolves in one round trip per task instead of a real 2s wait per retry. The invariant
    under test is the atomic script's serialization: whichever K of the N happen to be the first K
    EVALs the server actually executes succeed, the rest see a count over the limit, compensate
    with a real DECR, and are refused -- and the counter is left at exactly K, not stuck above it
    from an uncompensated over-shoot.
    """
    n, k = 12, 5

    tasks = [asyncio.create_task(check_rate_limit(byte_mode_redis, k, sleep=_refuse_and_stop)) for _ in range(n)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successes = [r for r in results if r is None]
    refusals = [r for r in results if isinstance(r, _RateLimited)]
    assert len(successes) == k, f"expected exactly {k} successes, got {len(successes)} of {n}"
    assert len(refusals) == n - k
    assert int(await byte_mode_redis.get(_PROD_RATE_LIMIT_KEY)) == k, "the counter must settle at K, not above it"


async def test_concurrent_raw_evals_never_lose_or_duplicate_an_increment(scratch_key: str, byte_mode_redis: redis_async.Redis) -> None:
    """N concurrent raw EVALs on a fresh key: every count 1..N appears exactly once.

    This isolates the atomicity claim from ``check_rate_limit``'s retry loop: the script alone,
    hit concurrently by N real connections, must hand out N distinct, contiguous counts with no
    lost update and no duplicate -- what a Python-level fake (single interpreter, cooperative
    scheduling) cannot meaningfully test even if it tries to.
    """
    n = 25

    async def _one_eval() -> int:
        return int(await byte_mode_redis.eval(_RATE_LIMIT_LUA, 1, scratch_key, _RATE_LIMIT_WINDOW_SEC))

    before = await _server_ms(byte_mode_redis)
    counts = await asyncio.gather(*(_one_eval() for _ in range(n)))
    after = await _server_ms(byte_mode_redis)

    assert sorted(counts) == list(range(1, n + 1))
    assert int(await byte_mode_redis.get(scratch_key)) == n
    await _assert_armed_for(byte_mode_redis, scratch_key, _RATE_LIMIT_WINDOW_SEC, between=(before, after))
