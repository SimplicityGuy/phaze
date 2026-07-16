"""Canonical per-stage control constants (DB-free, agent-boundary-safe) -- Phase 37.

Single source of truth for the three agent pipeline stages, their registered SAQ
function names, and the pause "park" sentinel. Every downstream consumer imports
these EXACT constants rather than re-deriving them:

- the before-enqueue ``apply_stage_control`` hook (Plan 37-02) -- stamps new jobs,
- the pause endpoint (Plan 37-04) -- parks the queued backlog with ``scheduled = SENTINEL``,
- the resume guard (Plan 37-04) -- un-parks ONLY rows whose ``scheduled == SENTINEL``.

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
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog


if TYPE_CHECKING:
    from typing import Any

    from saq import Job


logger = structlog.get_logger(__name__)


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


__all__ = ["SENTINEL", "STAGE_TO_FUNCTION", "apply_stage_control"]
