"""Unit tests for the ``apply_stage_control`` before-enqueue hook (Phase 37 Plan 02) and the
``enforce_stage_pause_on_process`` / ``repark_if_stage_paused`` before/after-process pair
(phaze-geuq).

The before-enqueue hook stamps a new stage job with its stage's live ``priority`` (and parks
it with ``scheduled = SENTINEL`` when the stage is paused) by reading the
``pipeline_stage_control`` table through the queue's psycopg3 ``pool`` (NOT SQLAlchemy --
agent import boundary, 37-RESEARCH Pitfall 4). It mirrors :func:`apply_deterministic_key`'s
best-effort discipline: any control-read failure logs and returns without mutating, so an
enqueue is never blocked.

Five behaviors are proven with a fake queue exposing a fake ``.pool`` (an async
context-manager connection returning ``(paused, priority)``):

1. stamp     -- ``(paused=False, priority=37)`` => ``job.priority == 37``, ``scheduled`` unchanged;
2. park      -- ``(paused=True, priority=37)``  => ``job.priority == 37`` AND ``scheduled == SENTINEL``;
3. passthrough -- a non-stage function is untouched and triggers NO pool read;
4. best-effort -- a pool whose connection raises => warning logged, defaults left, no raise;
5. TTL cache -- two enqueues of the same stage within the TTL window issue ONE pool read.

phaze-geuq: SAQ's retry path (``PostgresQueue._retry``) re-queues via a raw ``update()`` that
never calls ``enqueue()``, so the before-enqueue hook above cannot see a retried job. The
before/after-process pair closes that gap at the WORKER boundary instead (unit-tested here
with a fake queue that mimics SAQ's ``Queue.update`` attribute-setting semantics; the
end-to-end reproduction against a real Postgres broker + real SAQ ``_retry``/``Worker`` lives
in ``tests/integration/test_stage_pause_retry_bounce.py``):

6. bounce   -- paused stage => raises ``StagePausedRetry``, flags ``ctx``, parks ``scheduled``;
7. passthrough -- unpaused stage / non-stage function => no raise, no ``ctx`` flag;
8. best-effort -- a pool whose connection raises => no raise, job proceeds unpaused;
9. after-process no-op -- ``ctx`` unflagged => no queue mutation;
10. after-process authoritative -- flagged ``ctx`` => forces QUEUED/SENTINEL and restores
    ``attempts`` to its pre-dequeue count, even overriding an already-``FAILED`` status (the
    attempts-exhausted edge case a pause bounce must never terminalize).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import uuid

import pytest
from saq.job import Job, Status

from phaze.tasks._shared import stage_control
from phaze.tasks._shared.stage_control import (
    SENTINEL,
    StagePausedRetry,
    apply_stage_control,
    enforce_stage_pause_on_process,
    repark_if_stage_paused,
)


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


# ----------------------------------------------------------------------------------------
# phaze-geuq: enforce_stage_pause_on_process (before_process) / repark_if_stage_paused
# (after_process) -- covers SAQ's `_retry` before_enqueue bypass.
# ----------------------------------------------------------------------------------------


class _FakeUpdatingQueue:
    """Minimal stand-in for ``saq.queue.base.Queue`` mimicking ONLY ``update()``'s attribute-
    setting semantics (``base.py``'s real ``update()`` does the identical ``setattr`` loop
    before persisting) -- enough to unit-test the hooks without a real Postgres broker. The
    real end-to-end persistence (and SAQ's OWN ``_retry``) is covered by the integration
    regression in ``tests/integration/test_stage_pause_retry_bounce.py``.
    """

    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool
        self.update_calls: list[dict[str, Any]] = []

    async def update(self, job: Job, **kwargs: Any) -> None:
        self.update_calls.append(dict(kwargs))
        for k, v in kwargs.items():
            if hasattr(job, k):
                setattr(job, k, v)


def _ctx_for(job: Job) -> dict[str, Any]:
    return {"job": job}


async def test_enforce_stage_pause_on_process_bounces_when_paused() -> None:
    job = _stage_job("process_file")
    job.queue = _queue_with((True, 50))
    ctx = _ctx_for(job)

    with pytest.raises(StagePausedRetry):
        await enforce_stage_pause_on_process(ctx)

    assert ctx[stage_control._REPARK_CTX_KEY] is True
    assert job.scheduled == SENTINEL


async def test_enforce_stage_pause_on_process_noop_when_unpaused() -> None:
    job = _stage_job("process_file")
    job.queue = _queue_with((False, 50))
    ctx = _ctx_for(job)
    default_scheduled = job.scheduled

    await enforce_stage_pause_on_process(ctx)  # must NOT raise

    assert stage_control._REPARK_CTX_KEY not in ctx
    assert job.scheduled == default_scheduled


async def test_enforce_stage_pause_on_process_noop_for_non_stage_function() -> None:
    job = Job(function="heartbeat_tick", kwargs={})
    pool = _FakePool((True, 50))
    job.queue = SimpleNamespace(pool=pool)
    ctx = _ctx_for(job)

    await enforce_stage_pause_on_process(ctx)  # must NOT raise

    assert stage_control._REPARK_CTX_KEY not in ctx
    assert pool.read_count[0] == 0


async def test_enforce_stage_pause_on_process_best_effort_on_read_failure() -> None:
    job = _stage_job("fingerprint_file")
    job.queue = _queue_with(None, boom=True)
    ctx = _ctx_for(job)
    default_scheduled = job.scheduled

    await enforce_stage_pause_on_process(ctx)  # must NOT raise

    assert stage_control._REPARK_CTX_KEY not in ctx
    assert job.scheduled == default_scheduled


async def test_repark_if_stage_paused_noop_when_not_flagged() -> None:
    job = _stage_job("process_file")
    queue = _FakeUpdatingQueue(_FakePool((False, 50)))
    job.queue = queue
    ctx = _ctx_for(job)

    await repark_if_stage_paused(ctx)  # must NOT raise

    assert queue.update_calls == []


async def test_repark_if_stage_paused_restores_queued_state_and_attempts() -> None:
    job = _stage_job("process_file")
    queue = _FakeUpdatingQueue(_FakePool((False, 50)))
    job.queue = queue
    job.attempts = 2  # Worker.process() already incremented this before the bounce
    job.error = "timeout (simulated)"
    ctx = _ctx_for(job)
    ctx[stage_control._REPARK_CTX_KEY] = True

    await repark_if_stage_paused(ctx)

    assert job.status == Status.QUEUED
    assert job.scheduled == SENTINEL
    assert job.attempts == 1, "pause bounce must undo Worker.process()'s attempts increment"
    assert job.error is None
    assert stage_control._REPARK_CTX_KEY not in ctx  # popped, not just falsified


async def test_repark_if_stage_paused_overrides_an_already_failed_terminal_status() -> None:
    """The attempts-exhausted edge case: the generic exception handler already called
    ``job.finish(FAILED, ...)`` before this hook runs. A pause bounce must NEVER terminalize
    the job (it would silently orphan the deterministic-key scheduling-ledger row), so this
    hook's write must win regardless of what SAQ's own retry/finish decided in between.
    """
    job = _stage_job("process_file")
    queue = _FakeUpdatingQueue(_FakePool((False, 50)))
    job.queue = queue
    job.attempts = job.retries  # exhausted -> job.retryable was False, so finish() ran
    job.status = Status.FAILED
    ctx = _ctx_for(job)
    ctx[stage_control._REPARK_CTX_KEY] = True

    await repark_if_stage_paused(ctx)

    assert job.status == Status.QUEUED
    assert job.scheduled == SENTINEL
    assert job.attempts == job.retries - 1
