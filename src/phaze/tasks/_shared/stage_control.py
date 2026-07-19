"""Canonical per-stage control constants (DB-free, agent-boundary-safe) -- Phase 37.

Single source of truth for the three agent pipeline stages, their registered SAQ
function names, and the pause "park" sentinel. Every downstream consumer imports
these EXACT constants rather than re-deriving them:

- the before-enqueue ``apply_stage_control`` hook (Plan 37-02) -- stamps new jobs,
- the pause endpoint (Plan 37-04) -- parks the queued backlog with ``scheduled = SENTINEL``,
- the resume guard (Plan 37-04) -- un-parks ONLY rows whose ``scheduled == SENTINEL``,
- the before-PROCESS ``enforce_stage_pause_on_process`` / after-process
  ``repark_if_stage_paused`` pair (phaze-geuq) -- covers SAQ's ``_retry`` re-queue path,
  which ``before_enqueue`` structurally cannot see.

``STAGE_TO_FUNCTION`` maps each stage label to the registered SAQ function name
(``job.function``), verified against ``_KEY_BUILDERS`` in
:mod:`phaze.tasks._shared.deterministic_key`. The raw ``saq_jobs`` UPDATEs filter on
``key LIKE '<function>:%'`` (there is no ``function`` column), so this mapping is also
the source of the key prefixes.

``SENTINEL = 9999999999`` is a fixed epoch-seconds value (year 2286) that is far beyond
any legitimate ``scheduled`` (retry backoffs are ``now + small delay``; cron jobs are
``now + interval``). It is a SINGLE shared constant so the resume guard's
``scheduled == SENTINEL`` comparison is exact -- never recompute it per call (37-RESEARCH
SENTINEL value / Anti-Patterns).

CRITICAL boundary rule (37-RESEARCH Pitfall 4): this module must NOT import
``phaze.database``, ``phaze.tasks.session``, or ``sqlalchemy.ext.asyncio``. The
``apply_stage_control`` before-enqueue hook (Plan 02) reads the control table through the
queue's own psycopg3 ``pool`` -- NOT SQLAlchemy -- so the agent worker can import this
module without pulling the ORM/DB layer across the agent import boundary (covered by
``tests/shared/core/test_task_split.py``).

phaze-geuq -- SAQ's retry path bypasses ``before_enqueue`` entirely
---------------------------------------------------------------------
Verified against the INSTALLED SAQ 0.26.4 source (not assumed from docs):

- ``PostgresQueue._retry`` (``saq/queue/postgres.py:816-823``) re-queues a failed/timed-out
  attempt via ``self.update(job, status=Status.QUEUED, scheduled=...)`` -- it NEVER calls
  ``Queue.enqueue()``.
- ``Queue._before_enqueue`` callbacks (registered via ``register_before_enqueue``, which is
  how ``apply_stage_control`` is wired in ``queue_factory.py``) are dispatched ONLY from
  ``Queue.enqueue()`` (``saq/queue/base.py`` -- there is no other call site).

So a stage pause parks the QUEUED backlog (``_PAUSE_SQL`` in ``phaze.services.stage_control``,
one-shot over ``status = 'queued'`` at pause time) and ``apply_stage_control`` parks every
NEW enqueue -- but neither touches a job that was ACTIVE at pause time and later retried:
``_retry`` leaves ``retry_delay = 0.0`` / ``retry_backoff = False`` (project defaults, never
overridden) untouched, so ``next_retry_delay()`` returns ``0.0`` and the retried row is
scheduled at (approximately) ``now()`` -- it dequeues on the very next poll, on a stage the
operator believes is paused. Widening ``_PAUSE_SQL`` to also match ``status = 'active'``
CANNOT fix this: it is a one-shot UPDATE issued once, at pause time, over rows that exist
THEN; a job that fails and retries AFTER the pause UPDATE has already run was never touched
by it and re-enters ``status = 'queued'`` through a completely different code path
(``_retry``, not the pause endpoint) that the one-shot UPDATE cannot anticipate.

The chosen fix is a SECOND enforcement point at the WORKER's ``before_process`` /
``after_process`` boundary (``saq/worker.py:341-438``), which runs on every dequeued job
regardless of whether it arrived via ``enqueue()`` or ``_retry()``'s raw ``update()``:

- :func:`enforce_stage_pause_on_process` (``before_process``) re-checks ``paused`` for the
  job's stage the instant BEFORE its function would run and, if paused, immediately reparks
  the job (``status = QUEUED``, ``scheduled = SENTINEL``) and raises so the function body
  never executes -- covering the retry-bypass gap ``before_enqueue`` cannot see.
- :func:`repark_if_stage_paused` (``after_process``) runs in the Worker's ``finally`` on
  EVERY outcome (success, retry, or failure) and, ONLY when the paired before-hook flagged a
  bounce on this ``ctx``, forcibly re-asserts the parked state (including restoring
  ``job.attempts`` to its pre-dequeue count). This is intentionally the LAST word regardless
  of what the generic exception handler decided in between (``job.retry`` or, in the
  attempts-exhausted edge case, ``job.finish(FAILED)``): a pause bounce must NEVER consume
  retry budget or terminalize the job. Terminalizing here would silently orphan the
  ``deterministic_key.py`` scheduling-ledger row -- that key is a correct IN-FLIGHT guard but
  a WRONG tombstone, since neither ``put_analysis`` nor ``report_analysis_failed`` (the only
  callbacks that clear it) is ever invoked for a bounce that never ran the task body, and
  normal recovery would never pick the file back up again.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from saq.job import Status
import structlog


if TYPE_CHECKING:
    from saq import Job


logger = structlog.get_logger(__name__)

# ``ctx`` key the before/after-process hook pair uses to hand off "this attempt was bounced
# by a stage pause" across the Worker's function-call boundary (worker.py:360-421). Private to
# this module -- no other code should read or set it.
_REPARK_CTX_KEY = "_phaze_stage_pause_repark"


class StagePausedRetry(Exception):
    """Raised by :func:`enforce_stage_pause_on_process` to abort a bounced retry before it runs.

    Internal control-flow signal only -- never surfaces past SAQ's Worker.process(), which
    catches it as a generic exception and (harmlessly) calls ``job.retry``/``job.finish``;
    :func:`repark_if_stage_paused` overwrites whatever that decided with the correct parked
    state on the SAME ``ctx`` in the ``finally`` block that follows.
    """


# Stage label -> registered SAQ function name (job.function). Verified against
# _KEY_BUILDERS in phaze.tasks._shared.deterministic_key. The deterministic key form
# is "<function>:<file_id>", so the saq_jobs filter is `key LIKE '<function>:%'`.
STAGE_TO_FUNCTION: dict[str, str] = {
    "metadata": "extract_file_metadata",
    "analyze": "process_file",
    "fingerprint": "fingerprint_file",
}

# Exact inverse: registered function name -> stage label. Used by the enqueue hook to
# resolve job.function back to its stage (non-stage jobs map to None and are untouched).
_FUNCTION_TO_STAGE: dict[str, str] = {v: k for k, v in STAGE_TO_FUNCTION.items()}

# Pause "park" value: a far-future epoch-seconds timestamp (2286-11-20). A queued job
# with scheduled = SENTINEL fails the dequeue's `now >= scheduled` gate, so it parks.
# Resume un-parks ONLY rows whose scheduled == SENTINEL, structurally protecting genuine
# retry backoffs (which use now + delay, never == SENTINEL).
SENTINEL: int = 9999999999


# In-process TTL cache for the 3-row control table. The before-enqueue hook runs on EVERY
# enqueue (including bulk runs of ~11k files), so a naive read-through would add one SELECT
# per enqueue. A tiny module-level cache keyed by stage with a single monotonic expiry
# collapses a bulk enqueue to ~1 SELECT per stage per TTL window. Staleness is bounded and
# harmless: the pause/priority endpoints issue the bulk saq_jobs UPDATE for the EXISTING
# backlog, so the hook only stamps NEW jobs -- a <=5s lag before new jobs pick up a
# just-changed priority is operationally invisible (37-RESEARCH Pitfall 5 / Open-Q2). TTL is
# tunable; a single-user admin tool tolerates the bounded staleness.
_CACHE_TTL_SECONDS: float = 5.0
_cache: dict[str, tuple[bool, int]] = {}
_cache_expires_at: float = 0.0


async def _read_stage_control(queue: Any, stage: str) -> tuple[bool, int]:
    """Return ``(paused, priority)`` for ``stage`` via the queue's psycopg3 pool (TTL-cached).

    Reads ``pipeline_stage_control`` through ``queue.pool`` (psycopg3 -- the SAME open
    PostgresQueue pool SAQ's own ``_enqueue`` uses) so the hook never imports the SQLAlchemy
    engine / ``phaze.database`` (37-RESEARCH Pitfall 4, agent import boundary). A module-level
    cache keyed by stage with a single monotonic ``_cache_expires_at`` window collapses
    bulk-enqueue reads; on window expiry the whole cache is dropped and a fresh window opens.
    Uses psycopg3 ``%(name)s`` paramstyle with a bound param -- never an f-string (T-37-01).
    """
    global _cache_expires_at
    now = time.monotonic()
    if now < _cache_expires_at and stage in _cache:
        return _cache[stage]
    async with queue.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT paused, priority FROM pipeline_stage_control WHERE stage = %(stage)s",
            {"stage": stage},
        )
        row = await cursor.fetchone()
    if row is None:
        # No control row for this stage (pre-migration 020 / manually-deleted row). Best-effort:
        # enqueue unpaused at the mid-range default rather than subscripting None into the caller's
        # broad ``except`` as a misleading "read failed". NOT cached, so a freshly-seeded row is
        # picked up on the next enqueue instead of after the TTL window (WR-01).
        logger.warning("stage-control row missing; enqueuing unpaused/default", stage=stage)
        return (False, 50)
    result = (bool(row[0]), int(row[1]))
    if now >= _cache_expires_at:
        # Window elapsed: open a fresh one and drop any stale entries from the prior window.
        _cache.clear()
        _cache_expires_at = now + _CACHE_TTL_SECONDS
    _cache[stage] = result
    return result


async def apply_stage_control(job: Job) -> None:
    """SAQ ``before_enqueue`` hook -- stamp a new stage job with its live priority + park.

    Registered in :func:`phaze.tasks._shared.queue_factory.build_pipeline_queue` AFTER
    ``apply_deterministic_key`` (so ``job.function`` is final). For a job whose function maps
    to one of the three agent stages, reads the stage's ``(paused, priority)`` from the
    TTL-cached control table and sets ``job.priority``; if the stage is paused, ALSO parks the
    job by setting ``job.scheduled = SENTINEL`` so it fails the dequeue's ``now >= scheduled``
    gate (REQ-37-1). Non-stage jobs are left completely untouched.

    Best-effort (mirrors :func:`apply_deterministic_key`): ANY control-read failure logs a
    warning and returns without mutating, so the job enqueues at the default/unpaused state.
    A control-table hiccup must NEVER block an enqueue (37-RESEARCH Pitfall 4 / T-37-02).
    """
    stage = _FUNCTION_TO_STAGE.get(job.function)
    if stage is None:
        return
    try:
        paused, priority = await _read_stage_control(job.queue, stage)
    except Exception:
        logger.warning("stage-control read failed; enqueuing unpaused/default", function=job.function, exc_info=True)
        return
    job.priority = priority
    if paused:
        job.scheduled = SENTINEL


async def enforce_stage_pause_on_process(ctx: dict[str, Any]) -> None:
    """SAQ Worker ``before_process`` hook -- park a job of a PAUSED stage before it runs.

    Registered as a ``before_process`` Worker-constructor kwarg (NOT a ``register_*`` call --
    this hook lives on the Worker, not the Queue), it runs on EVERY dequeued job right before
    its function is invoked (``saq/worker.py:360``) -- including a job that reached
    ``status = 'queued'`` via SAQ's ``_retry`` re-queue path, which ``before_enqueue`` hooks
    (``apply_stage_control`` above) structurally cannot see (module docstring, phaze-geuq).

    For a job whose function maps to one of the three agent stages, re-reads the stage's
    ``paused`` flag (the SAME TTL-cached read ``apply_stage_control`` uses -- a bounded <=5s
    staleness is the project's accepted tolerance for stage-control propagation). If paused,
    mutates ``job.scheduled = SENTINEL`` IN-MEMORY (no DB write here -- ``repark_if_stage_paused``
    does the one authoritative write) so that if the generic exception handler falls through to
    ``job.retry()``, SAQ's own ``_retry`` (``scheduled = job.scheduled or now_seconds()``) computes
    the SAME parked value rather than ``now_seconds()``, flags the bounce on ``ctx`` for the
    after-process pass to pick up, and raises :class:`StagePausedRetry` so ``Worker.process``
    never calls the actual task function (no fresh full-length analysis attempt runs).

    Best-effort read (mirrors :func:`apply_stage_control`): a control-read failure logs a
    warning and returns without mutating, so a control-table hiccup can never wedge a job that
    should otherwise run.
    """
    job: Job = ctx["job"]
    stage = _FUNCTION_TO_STAGE.get(job.function)
    if stage is None:
        return
    try:
        paused, _priority = await _read_stage_control(job.queue, stage)
    except Exception:
        logger.warning("stage-control read failed at dequeue time; processing job unpaused", function=job.function, exc_info=True)
        return
    if not paused:
        return
    logger.info(
        "stage paused; reparking dequeued/retried job before execution instead of running a fresh attempt",
        function=job.function,
        key=job.key,
        stage=stage,
    )
    ctx[_REPARK_CTX_KEY] = True
    job.scheduled = SENTINEL
    raise StagePausedRetry(f"stage {stage!r} paused; job {job.key} reparked before execution")


async def repark_if_stage_paused(ctx: dict[str, Any]) -> None:
    """SAQ Worker ``after_process`` hook -- the LAST word on a pause-bounced attempt.

    Registered as an ``after_process`` Worker-constructor kwarg, this runs in the Worker's
    ``finally`` block on EVERY outcome (``saq/worker.py:422-437``) -- success, retry, or
    failure alike -- so it is guaranteed to run immediately after whatever
    :func:`enforce_stage_pause_on_process`'s raised :class:`StagePausedRetry` caused the
    generic exception handler to decide (``job.retry`` in the ordinary case; in the edge case
    where ``job.attempts`` was already at its ``retries`` ceiling, ``job.finish(FAILED)``).

    A no-op UNLESS the paired before-hook flagged this exact ``ctx`` (``_REPARK_CTX_KEY``).
    When flagged, forcibly re-asserts the parked state -- ``status = QUEUED``,
    ``scheduled = SENTINEL`` -- and restores ``job.attempts`` to its PRE-dequeue count
    (``Worker.process`` increments ``job.attempts`` before ``before_process`` runs; undoing
    that here means a pause bounce NEVER consumes retry budget). This authoritatively
    overrides a ``FAILED`` terminal outcome too: a pause bounce must never terminalize the
    job, because that would silently orphan its ``deterministic_key.py`` scheduling-ledger
    row (a correct in-flight guard, a WRONG tombstone -- module docstring) with no HTTP
    callback ever clearing it, permanently blocking recovery from re-enqueuing the file.
    """
    if not ctx.pop(_REPARK_CTX_KEY, False):
        return
    job: Job = ctx["job"]
    restored_attempts = max(job.attempts - 1, 0)
    await job.update(status=Status.QUEUED, scheduled=SENTINEL, attempts=restored_attempts, error=None)


__all__ = [
    "SENTINEL",
    "STAGE_TO_FUNCTION",
    "StagePausedRetry",
    "apply_stage_control",
    "enforce_stage_pause_on_process",
    "repark_if_stage_paused",
]
