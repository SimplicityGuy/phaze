"""phaze-qmc2.1 calibration spike -- lock the installed SAQ (0.26.4) semantics that the
grx3 / e57w fixes are designed against.

This is the SPIKE's CI-safe reproduction: it asserts, against the *installed* SAQ, the four
load-bearing facts established empirically on prod (recorded in
``scripts/parity/fingerprint_timeout_probe.sql`` and on beads qmc2.1 / grx3 / e57w):

1. A claimed-but-unrun row (``attempts == 0``, ``started``/``touched`` frozen at dequeue) IS
   ``stuck`` once ``timeout`` elapses -- ``Job.stuck`` keys off ``started``, NOT off whether the
   worker ever executed it. => the sweep WILL abort never-run buffered rows.
2. Such a row does NOT burn retry budget on the sweep: ``retryable`` stays True while
   ``attempts < retries``, so the sweep ``retry()``s it back to QUEUED rather than aborting.
   ``attempts`` is bumped ONLY by the worker's process loop (saq/worker.py:356), never by
   dequeue or sweep.
3. ``Job.to_dict()`` OMITS ``attempts`` when it is 0 -- so an ABSENT ``attempts`` key in a
   persisted ``saq_jobs`` blob is a reliable "never executed by a worker" signal. This is the
   discriminator the prod probe used (2 running vs 3447 claimed-but-unrun).
4. ``Queue.update()`` overwrites ``touched`` with ``now()`` -- so ``touched`` advancement is NOT
   evidence of genuine work; the sweeper's own abort->ABORTING update bumps it. This refutes the
   "the zombie did ~53 min of genuine work" premise behind the 600s-is-too-short question.

If a future SAQ bump changes any of these, this test fails loudly and the fixes must be
re-calibrated before trusting the old conclusions.
"""

from __future__ import annotations

import types
from typing import cast

import pytest
from saq.job import Job, Status
from saq.queue.base import Queue
from saq.utils import now


def _claimed_but_unrun_job(*, timeout: int = 600, retries: int = 4) -> Job:
    """A row as SAQ leaves it right after ``_dequeue``: ACTIVE, started==touched==dequeue, attempts=0."""
    dequeued = now() - (timeout + 120) * 1000  # ms; comfortably past the timeout
    return Job(
        function="fingerprint_file",
        kwargs={"file_id": "11111111-1111-1111-1111-111111111111"},
        status=Status.ACTIVE,
        timeout=timeout,
        retries=retries,
        attempts=0,
        started=dequeued,
        touched=dequeued,
        heartbeat=0,
    )


def test_claimed_but_unrun_row_is_stuck_via_started() -> None:
    """Fact 1: a never-run ACTIVE row past its timeout is ``stuck`` even with attempts=0/no heartbeat."""
    job = _claimed_but_unrun_job()

    assert job.attempts == 0
    assert job.heartbeat == 0  # no heartbeat branch -> stuck is decided purely by started vs timeout
    assert job.stuck is True


def test_fresh_claimed_row_within_timeout_is_not_stuck() -> None:
    """Guard: a just-dequeued row (within timeout) is NOT stuck -- the sweep leaves it alone."""
    job = _claimed_but_unrun_job()
    job.started = now()
    job.touched = job.started

    assert job.stuck is False


def test_sweep_of_unrun_row_does_not_burn_retry_budget() -> None:
    """Fact 2: attempts=0 < retries -> retryable, so a swept never-run row is retried, not aborted."""
    job = _claimed_but_unrun_job(retries=4)

    # attempts is untouched by dequeue/sweep; it is only bumped by worker.process().
    assert job.attempts == 0
    assert job.retryable is True

    # Only a row that actually executed to exhaustion (attempts == retries) is non-retryable and
    # would be finish(ABORTED)'d by the sweep.
    job.attempts = job.retries
    assert job.retryable is False


def test_absent_attempts_key_is_the_never_executed_signal() -> None:
    """Fact 3: to_dict() omits attempts when 0 -- the exact signal the prod probe relied on."""
    unrun = _claimed_but_unrun_job()
    assert "attempts" not in unrun.to_dict()  # attempts == 0 -> omitted

    ran = _claimed_but_unrun_job()
    ran.attempts = 1
    assert ran.to_dict().get("attempts") == 1  # attempts >= 1 -> present == genuinely executed


@pytest.mark.asyncio
async def test_queue_update_bumps_touched_regardless_of_progress() -> None:
    """Fact 4: Queue.update() overwrites touched=now(), so touched advancement != genuine work.

    Exercises the real ``saq.queue.base.Queue.update`` against a minimal stub self (identity
    ``copy`` + no-op ``_update``) so no Postgres/pool is required. The sweeper's abort->ABORTING
    path routes through this same method, which is why a never-run zombie's ``touched`` can crawl
    hours past ``started`` with attempts still 0.
    """
    job = _claimed_but_unrun_job()
    frozen = job.touched

    async def _noop_update(_job: Job, **_kw: object) -> None:
        return None

    stub = types.SimpleNamespace(copy=lambda j: j, _update=_noop_update)

    await Queue.update(cast("Queue", stub), job, status=Status.ABORTING)

    assert job.status == Status.ABORTING
    assert job.touched > frozen  # bumped by the update itself, not by any fingerprint heartbeat
    assert job.attempts == 0  # ...while attempts is still 0: no genuine execution happened
