"""phaze-geuq REGRESSION: SAQ's real ``_retry`` re-queue path bypasses ``before_enqueue``.

Reproduces, against a real Postgres ``saq_jobs`` broker and SAQ's OWN ``PostgresQueue._retry``
(``saq/queue/postgres.py:816-823``), the exact scenario 37/geuq documents:

1. a ``process_file`` job goes ACTIVE (a real worker dequeue);
2. the operator pauses ``analyze`` WHILE it is in flight -- drain semantics (REQ-37-1) leave
   the active row untouched, exactly as designed;
3. the job times out and SAQ's OWN retry path (``queue._retry``, never ``enqueue()`` /
   ``before_enqueue``) re-queues it at (approximately) ``now()`` because this project leaves
   ``retry_delay = 0.0`` / ``retry_backoff = False`` untouched;
4. a real ``saq.Worker.process()`` dequeue+dispatch cycle picks the retried row back up.

``test_control_group_without_hooks_reproduces_the_bug`` proves step 4 genuinely runs a fresh
attempt through real SAQ machinery (no fix applied) -- so the fix under test in
``test_paused_stage_does_not_run_a_retried_active_job`` is proven against the actual defect,
not a mock that merely fails to exercise it. Both tests fail to even COLLECT before the fix
lands (``enforce_stage_pause_on_process`` / ``repark_if_stage_paused`` do not exist yet), and
the first fails its assertions outright once those names exist but the wiring is skipped.

Run with real PG via ``just integration-test``; the package is auto-marked ``integration``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest
from saq import Worker
from sqlalchemy import text

from phaze.services.stage_control import pause_stage
from phaze.tasks._shared import stage_control as stage_control_module
from phaze.tasks._shared.stage_control import SENTINEL, enforce_stage_pause_on_process, repark_if_stage_paused


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

_ROW_SQL = text("SELECT status, scheduled FROM saq_jobs WHERE key = :k")


async def _fake_process_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Stand-in for the real (up to ~2h) analysis body -- records that it actually ran."""
    calls: list[str] = ctx["_process_calls"]
    calls.append(kwargs["file_id"])
    return {"status": "ran"}


async def _row(session: AsyncSession, key: str) -> tuple[str, int]:
    result = (await session.execute(_ROW_SQL, {"k": key})).one()
    return result.status, int(result.scheduled)


async def _prime_active_then_pause_then_retry(
    queue: PostgresQueue,
    session_factory: async_sessionmaker[AsyncSession],
    file_id: str,
) -> str:
    """Steps 1-3 of the reproduction, shared by both the fixed and control-group tests.

    Returns the deterministic ``saq_jobs`` key. Leaves the row ``status='queued'`` with
    ``scheduled`` set to (approximately) now -- SAQ's own retry path re-queued it while
    ``analyze`` is paused, exactly the pre-fix hazard.
    """
    await queue.enqueue("process_file", file_id=file_id)
    key = f"process_file:{file_id}"

    # (1) Simulate a real worker dequeue: the row goes ACTIVE.
    active_job = await queue.dequeue(timeout=2.0)
    assert active_job is not None
    assert active_job.key == key

    # (2) Operator pauses `analyze` WHILE the job is active -- mirrors the real pause endpoint
    # (routers/pipeline_stages.py: `row.paused = True; await pause_stage(session, stage)` in ONE
    # transaction): flip the durable intent row AND park the queued backlog. pause_stage's
    # one-shot UPDATE only touches status='queued' rows (REQ-37-1 drain semantics), so the
    # active row is untouched.
    async with session_factory() as session:
        await session.execute(text("UPDATE pipeline_stage_control SET paused = true WHERE stage = 'analyze'"))
        await pause_stage(session, "analyze")
        await session.commit()
    # The stage-control read is TTL-cached (<=5s, an accepted enqueue-propagation lag) and the
    # `queue.enqueue()` call above already primed it unpaused. Drop it so the retry-time
    # before_process read below observes the pause we just committed -- exactly what a real
    # operator waiting past the TTL window (or a fresh worker process) would see.
    stage_control_module._cache.clear()
    stage_control_module._cache_expires_at = 0.0

    # (3) The job times out; SAQ's OWN `_retry` re-queues it -- the exact before_enqueue bypass
    # this bead fixes (saq/queue/postgres.py:816-823), never going through enqueue().
    await queue._retry(active_job, error="timeout (simulated)")

    async with session_factory() as session:
        status, scheduled = await _row(session, key)
    # Sanity: SAQ's own retry path really did requeue it for (approximately) immediate pickup.
    assert status == "queued"
    assert scheduled < SENTINEL
    return key


async def test_paused_stage_does_not_run_a_retried_active_job(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """FIXED: the before/after-process hook pair bounces the retried job before it runs."""
    queue, session_factory = stage_env
    file_id = str(uuid.uuid4())
    calls: list[str] = []

    key = await _prime_active_then_pause_then_retry(queue, session_factory, file_id)

    worker = Worker(
        queue=queue,
        functions=[("process_file", _fake_process_file)],
        before_process=[enforce_stage_pause_on_process],
        after_process=[repark_if_stage_paused],
        concurrency=1,
        dequeue_timeout=2.0,
    )
    # Worker.process() merges self.context into every job's ctx (worker.py:359) -- smuggle the
    # shared call-log in through that same seam rather than reaching into SAQ internals.
    worker.context["_process_calls"] = calls

    # (4) A real dequeue+dispatch cycle must NOT run the task function.
    processed = await worker.process()
    assert processed is True  # a job WAS dequeued and handled (bounced), not "queue empty"
    assert calls == [], f"paused stage ran a fresh attempt of a retried job: {calls}"

    async with session_factory() as session:
        status, scheduled = await _row(session, key)
    assert status == "queued"
    assert scheduled == SENTINEL  # reparked, not left at the retry's ~now() schedule

    # The bounce must NOT consume retry budget -- it never really "attempted" anything.
    refreshed = await queue.job(key)
    assert refreshed is not None
    assert refreshed.attempts == 0, f"pause bounce consumed retry budget: attempts={refreshed.attempts}"


async def test_control_group_without_hooks_reproduces_the_bug(
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
) -> None:
    """CONTROL: the SAME real-SAQ scenario, WITHOUT the fix's hooks, runs a fresh attempt.

    Proves the harness genuinely exercises the documented defect via real SAQ machinery (not
    a mock that merely fails to trigger it): with no ``before_process``/``after_process``
    registered, ``Worker.process()`` runs the task function on a stage the operator paused.
    """
    queue, session_factory = stage_env
    file_id = str(uuid.uuid4())
    calls: list[str] = []

    await _prime_active_then_pause_then_retry(queue, session_factory, file_id)

    worker = Worker(
        queue=queue,
        functions=[("process_file", _fake_process_file)],
        concurrency=1,
        dequeue_timeout=2.0,
    )
    worker.context["_process_calls"] = calls

    processed = await worker.process()
    assert processed is True
    assert calls == [file_id], "control-group scenario did not reproduce the documented defect"
