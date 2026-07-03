"""Unit tests for the ``apply_stage_control`` before-enqueue hook (Phase 37 Plan 02).

The hook stamps a new stage job with its stage's live ``priority`` (and parks it with
``scheduled = SENTINEL`` when the stage is paused) by reading the ``pipeline_stage_control``
table through the queue's psycopg3 ``pool`` (NOT SQLAlchemy -- agent import boundary,
37-RESEARCH Pitfall 4). It mirrors :func:`apply_deterministic_key`'s best-effort discipline:
any control-read failure logs and returns without mutating, so an enqueue is never blocked.

Five behaviors are proven with a fake queue exposing a fake ``.pool`` (an async
context-manager connection returning ``(paused, priority)``):

1. stamp     -- ``(paused=False, priority=37)`` => ``job.priority == 37``, ``scheduled`` unchanged;
2. park      -- ``(paused=True, priority=37)``  => ``job.priority == 37`` AND ``scheduled == SENTINEL``;
3. passthrough -- a non-stage function is untouched and triggers NO pool read;
4. best-effort -- a pool whose connection raises => warning logged, defaults left, no raise;
5. TTL cache -- two enqueues of the same stage within the TTL window issue ONE pool read.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import uuid

import pytest
from saq.job import Job

from phaze.tasks._shared import stage_control
from phaze.tasks._shared.stage_control import SENTINEL, apply_stage_control


class _FakeCursor:
    """Minimal psycopg3-cursor stand-in: ``fetchone`` returns the seeded row."""

    def __init__(self, row: tuple[bool, int] | None) -> None:
        self._row = row

    async def fetchone(self) -> tuple[bool, int] | None:
        return self._row


class _FakeConn:
    """psycopg3-connection stand-in: ``execute`` bumps the shared read counter."""

    def __init__(self, row: tuple[bool, int] | None, counter: list[int]) -> None:
        self._row = row
        self._counter = counter

    async def execute(self, sql: str, params: dict[str, Any]) -> _FakeCursor:
        # Record the call so the TTL-cache test can assert a single underlying read, and
        # confirm the hook binds the stage as a psycopg3 %(name)s param (never an f-string).
        assert "%(stage)s" in sql
        assert "stage" in params
        self._counter[0] += 1
        return _FakeCursor(self._row)


class _ConnCtx:
    """Async-context-manager wrapper for ``pool.connection()``; raises when ``boom``."""

    def __init__(self, conn: _FakeConn, *, boom: bool) -> None:
        self._conn = conn
        self._boom = boom

    async def __aenter__(self) -> _FakeConn:
        if self._boom:
            raise RuntimeError("pool down")
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    """psycopg3-pool stand-in exposing ``connection()`` + a read counter."""

    def __init__(self, row: tuple[bool, int] | None, *, boom: bool = False) -> None:
        self._row = row
        self._boom = boom
        self.read_count: list[int] = [0]

    def connection(self) -> _ConnCtx:
        return _ConnCtx(_FakeConn(self._row, self.read_count), boom=self._boom)


def _stage_job(function: str) -> Job:
    return Job(function=function, kwargs={"file_id": uuid.uuid4()})


def _queue_with(row: tuple[bool, int] | None, *, boom: bool = False) -> SimpleNamespace:
    return SimpleNamespace(pool=_FakePool(row, boom=boom))


@pytest.fixture(autouse=True)
def _reset_cache() -> Any:
    # The hook caches control state in module-level globals; reset before AND after every
    # test so the TTL-cache test (and all others) are deterministic regardless of ordering.
    stage_control._cache.clear()
    stage_control._cache_expires_at = 0.0
    yield
    stage_control._cache.clear()
    stage_control._cache_expires_at = 0.0


async def test_stamp_sets_priority_leaves_scheduled() -> None:
    job = _stage_job("process_file")  # analyze stage
    job.queue = _queue_with((False, 37))
    default_scheduled = job.scheduled
    await apply_stage_control(job)
    assert job.priority == 37
    assert job.scheduled == default_scheduled


async def test_park_sets_priority_and_sentinel_when_paused() -> None:
    job = _stage_job("process_file")
    job.queue = _queue_with((True, 37))
    await apply_stage_control(job)
    assert job.priority == 37
    assert job.scheduled == SENTINEL


async def test_passthrough_non_stage_job_untouched_and_no_read() -> None:
    job = Job(function="heartbeat_tick", kwargs={})
    pool = _FakePool((False, 50))
    job.queue = SimpleNamespace(pool=pool)
    default_priority, default_scheduled = job.priority, job.scheduled
    await apply_stage_control(job)
    assert job.priority == default_priority
    assert job.scheduled == default_scheduled
    assert pool.read_count[0] == 0


async def test_best_effort_read_failure_leaves_defaults() -> None:
    job = _stage_job("fingerprint_file")
    default_priority, default_scheduled = job.priority, job.scheduled
    job.queue = _queue_with(None, boom=True)
    await apply_stage_control(job)  # must NOT raise
    assert job.priority == default_priority
    assert job.scheduled == default_scheduled


async def test_ttl_cache_collapses_repeat_reads() -> None:
    pool = _FakePool((False, 42))
    queue = SimpleNamespace(pool=pool)
    j1, j2 = _stage_job("process_file"), _stage_job("process_file")
    j1.queue = queue
    j2.queue = queue
    await apply_stage_control(j1)
    await apply_stage_control(j2)
    assert j1.priority == 42
    assert j2.priority == 42
    assert pool.read_count[0] == 1
