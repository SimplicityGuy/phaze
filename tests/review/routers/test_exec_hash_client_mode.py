"""The ``exec:{batch_id}`` hash's bytes-vs-str Redis client-mode boundary, crossed against REAL Redis (phaze-ooe68, seam E3).

WHY THIS FILE EXISTS, AND WHY ``test_execution_dispatch.py`` DOES NOT COVER IT
------------------------------------------------------------------------------
``tests/review/routers/test_execution_dispatch.py`` DOES round-trip this hash -- ``dispatch_summary``
included -- through real Redis. What it never crosses is the client-mode boundary: its
``redis_client`` fixture is ``decode_responses=True`` and is handed to BOTH the writer and the
reader, so the two sides cannot disagree about bytes-vs-str. Sharing one client mode is exactly
what makes that round trip silent about this seam. Every test below keeps the two sides on
DIFFERENT modes, in both directions.

WHAT THE CODE-SIDE MEASUREMENT ACTUALLY FOUND (phaze-ooe68, 2026-08-24)
-----------------------------------------------------------------------
The bead's hypothesis was that production's writer for this hash "may be ``queue.cache_redis``",
the byte-mode handle from ``queue_factory.py:95``. Measured, it is not: every production toucher
of ``exec:*`` / ``execdispatch:*`` / ``exec_progress_req:*`` is ``app.state.redis``
(``main.py:161``, ``decode_responses=True``) --

* writer, seed:      ``services.execution_dispatch_protocol._seed_batch_hash`` (HSET+EXPIRE), via
                     ``DispatchDeps.redis_client`` <- ``routers/execution.py:347``;
* writer, reconcile: ``services.execution_dispatch_protocol._reconcile_undispatched`` (HSET/HINCRBY);
* writer, progress:  ``routers/agent_exec_batches.py:466`` (the ``_APPLY_INCREMENTS_LUA`` HINCRBY),
                     via ``_get_redis`` <- ``app.state.redis``;
* reader, UI/SSE:    ``routers/execution.py:409 _hgetall_or_empty`` -> ``_agents_view_from_hash``
                     -> ``_coerce_int`` / ``json.loads(dispatch_summary)``;
* reader, 404 probe: ``routers/agent_exec_batches.py:412,422`` ``HEXISTS``.

So this namespace is SINGLE-MODE by wiring, and the invariant is stated in the tree twice --
``routers/execution.py:19-20`` and the ``_get_redis`` docstring at ``agent_exec_batches.py:322-325``,
which explicitly contrasts the shared client with ``decode_responses=False``. Stated, and until
now never executed. That is what these tests convert into a running assertion: not "the crossing
happens in production" but "the single-mode wiring is LOAD-BEARING, and here is the failure it is
holding back". A stated invariant nothing exercises is one refactor away from being wrong.

Both directions are covered because they fail differently, and the difference is the finding:

* byte-mode WRITER -> str-mode reader: WORKS. HSET encodes identically either way, so client mode
  changes what a READ decodes to and nothing else. Writer mode is not load-bearing.
* str-mode writer -> byte-mode READER: silently COLLAPSES TO THE EMPTY STATE. Reader mode is.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from phaze.routers.agent_exec_batches import BATCH_KEY_PREFIX
from phaze.routers.execution import _agents_view_from_hash, _coerce_int, _hgetall_or_empty
from phaze.schemas.agent_tasks import ExecuteBatchProposalItem
from phaze.services.execution_dispatch_protocol import DispatchTally, _init_fields, _seed_batch_hash


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# Real Redis is mandatory here; these tests are about a real client's decoding.
pytestmark = pytest.mark.integration

_REDIS_URL = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")

_AGENT_A = "agent-alpha"
_AGENT_B = "agent-beta"


@pytest_asyncio.fixture
async def byte_mode_redis() -> AsyncGenerator[redis_async.Redis]:
    """A client wired EXACTLY as ``queue.cache_redis`` is (``queue_factory.py:95``): no ``decode_responses``."""
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def str_mode_redis() -> AsyncGenerator[redis_async.Redis]:
    """A client wired EXACTLY as ``app.state.redis`` is (``main.py:161``): ``decode_responses=True``.

    This is production's writer AND reader for ``exec:*`` -- see the module docstring.
    """
    client: redis_async.Redis = redis_async.Redis.from_url(_REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def batch_key() -> str:
    """A per-test ``exec:{batch_id}`` key.

    A fresh uuid per test, so teardown deletes exactly the key this test made. Deliberately NOT a
    ``scan_iter``+``delete`` sweep over ``exec:*``: CLAUDE.md records global sweeps in redis-backed
    fixtures as the mechanism by which one seat destroys another's live keys, and nothing here
    needs one.
    """
    return f"{BATCH_KEY_PREFIX}{uuid.uuid4()}"


@pytest_asyncio.fixture(autouse=True)
async def _drop_batch_key(str_mode_redis: redis_async.Redis, batch_key: str) -> AsyncGenerator[None]:
    yield
    await str_mode_redis.delete(batch_key)


def _proposal(index: int) -> ExecuteBatchProposalItem:
    """One payload item, built with the CURRENT field names.

    ``source_path`` was ``original_path`` until phaze-xzjrr, which renamed it as a deliberate
    breaking wire change; ``model_config = ConfigDict(extra="forbid")`` makes the old spelling a
    validation error rather than a silently ignored extra. Nothing here depends on the value --
    ``_init_fields`` only counts items -- so this is a plain constructor, but it has to be a
    VALID one for the seed mapping to be the real thing.
    """
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        source_path=f"/archive/<track-{index:02d}>.mp3",
        proposed_path="performances/artists/Example",
        proposed_filename=f"<track-{index:02d}>.mp3",
    )


def _seed_fields() -> dict[str, str]:
    """Build the seed mapping with the REAL producer, ``_init_fields`` -- not a hand-written dict.

    ADR-0012 rule 3: the artefact under test is the hash production actually writes, so the fields
    (``dispatch_summary``'s key set and ordering included -- a wire contract per its docstring)
    come from the function that writes them.
    """
    groups = {_AGENT_A: [_proposal(0), _proposal(1), _proposal(2)], _AGENT_B: [_proposal(3)]}
    plan = {agent_id: [items] for agent_id, items in groups.items()}
    tally = DispatchTally(batch_id=uuid.uuid4(), total=4, subjobs_expected=2, n_agents=2)
    return _init_fields(
        groups=groups,
        agent_names={_AGENT_A: "Alpha", _AGENT_B: "Beta"},
        plan=plan,
        tally=tally,
    )


async def test_byte_mode_writer_then_str_mode_reader_reads_the_hash_correctly(
    byte_mode_redis: redis_async.Redis,
    str_mode_redis: redis_async.Redis,
    batch_key: str,
) -> None:
    """Seed with the BYTE-mode client, read with production's STR-mode reader: the projection is intact.

    This is the direction the bead hypothesised was live in production. It is not (see the module
    docstring), and the reason it would be harmless if it ever became so is what this pins: ``HSET``
    stores the same bytes whichever mode the writing client is in, so writer mode is not part of
    this contract. That matters beyond this seam -- it is the same fact that removes the last
    argument for ``pipeline_counters._to_int`` tolerating byte-mode READS just because its writers
    are byte-mode (phaze-ooe68, seam E4).
    """
    fields = _seed_fields()
    await _seed_batch_hash(byte_mode_redis, batch_key, fields)

    data = await _hgetall_or_empty(str_mode_redis, batch_key)

    assert data["total"] == "4"
    assert data["status"] == "running"
    assert data["subjobs_expected"] == "2"
    assert _coerce_int(data.get("total")) == 4

    rows = _agents_view_from_hash(data, json.loads(data["dispatch_summary"]))
    assert [(r["agent_id"], r["name"], r["total"], r["completed"], r["failed"]) for r in rows] == [
        (_AGENT_A, "Alpha", 3, 0, 0),
        (_AGENT_B, "Beta", 1, 0, 0),
    ]


async def test_str_mode_writer_then_byte_mode_reader_collapses_to_the_empty_state(
    str_mode_redis: redis_async.Redis,
    byte_mode_redis: redis_async.Redis,
    batch_key: str,
) -> None:
    """Seed with production's writer, read with the WRONG client mode: every field silently reads as its default.

    This is the failure the single-mode wiring is holding back, and it is worth having on record
    because of HOW it fails. Nothing raises. ``_hgetall_or_empty`` returns a populated dict; it is
    keyed by ``bytes``, so every ``data.get("<str>")`` misses, ``_coerce_int`` returns its default
    for the ``None``, and the operator's live progress card renders a batch of 0 files at 0%
    complete with no agent rows -- indistinguishable from a reaped batch. A test that shared one
    client mode across both sides could never see this.

    The str-keyed lookups are spelled out rather than asserted only through the projection, so a
    future reader can see exactly which step loses the data.
    """
    await _seed_batch_hash(str_mode_redis, batch_key, _seed_fields())

    data = await _hgetall_or_empty(byte_mode_redis, batch_key)

    # The data IS there -- it just is not reachable by the str keys every consumer uses.
    assert data
    assert b"total" in data  # type: ignore[operator]
    assert data.get("total") is None
    assert data.get("dispatch_summary") is None
    assert _coerce_int(data.get("total")) == 0

    # And the projection the SSE tick and the reattach path both build degrades to nothing.
    assert _agents_view_from_hash(data, []) == []


async def test_the_two_clients_really_do_disagree_about_the_same_hash(
    str_mode_redis: redis_async.Redis,
    byte_mode_redis: redis_async.Redis,
    batch_key: str,
) -> None:
    """Guard on the harness itself: prove the fixtures are genuinely two DIFFERENT client modes.

    Without it, this whole file could silently degrade into the same-mode round trip it exists to
    replace, and would keep passing while proving nothing.
    """
    await _seed_batch_hash(str_mode_redis, batch_key, _seed_fields())

    assert await str_mode_redis.hget(batch_key, "status") == "running"
    assert await byte_mode_redis.hget(batch_key, "status") == b"running"


async def test_the_hexists_404_probe_is_client_mode_independent(
    str_mode_redis: redis_async.Redis,
    byte_mode_redis: redis_async.Redis,
    batch_key: str,
) -> None:
    """The OTHER reader of this hash -- ``agent_exec_batches``' 404 probe -- genuinely does not straddle.

    ``HEXISTS`` returns a protocol-level integer that redis-py converts to ``bool`` before any
    decoding step, so it reads the same through either client. Recording the negative result keeps
    the measurement honest: the reader half of ``agent_exec_batches`` is NOT a mode-sensitive
    consumer, and a future seat should not go looking for a defect there.
    """
    await _seed_batch_hash(str_mode_redis, batch_key, _seed_fields())

    for client in (str_mode_redis, byte_mode_redis):
        assert await client.hexists(batch_key, "total") is True
        assert await client.hexists(batch_key, f"agent:{_AGENT_A}:total") is True
        assert await client.hexists(batch_key, "agent:nobody:total") is False
