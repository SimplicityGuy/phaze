"""Central deterministic-key ``before_enqueue`` hook + completion ``after_process`` hook.

Generalizes the Phase-32 ``process_file:<file_id>`` deterministic-key fix to the WHOLE
pipeline, enforced at the single SAQ ``before_enqueue`` chokepoint so no call site can
drift back to a random-uuid key (D-05, locked decision A). The 2026-06-11 queue-doubling
incident proved that random-uuid jobs cannot dedup against a deterministic re-enqueue;
centralizing key construction here makes every routable task schedule-safe by construction.

Two hooks live here:

- :func:`apply_deterministic_key` (``before_enqueue``): for any function registered in
  :data:`_KEY_BUILDERS`, sets ``job.key = "<function>:<natural_id>"`` UNCONDITIONALLY
  (overriding any caller-supplied key -- anti-drift, threat T-35-01) and folds in the
  best-effort ``enqueued`` counter INCR. Functions absent from the registry keep SAQ's
  random-uuid default key.
- :func:`increment_completed` (``after_process``): bumps the ``completed`` counter only
  on a ``Status.COMPLETE`` terminal outcome.

The ``process_file`` builder MUST compute the IDENTICAL string the existing
:func:`phaze.services.analysis_enqueue.process_file_job_key` produces
(``process_file:<file_id>``), so the already-keyed path stays a no-op-equivalent
(35-RESEARCH Q1).

NOTE -- ACCEPTED enqueued upward drift (plan-checker W3): this hook runs PRE-dedup, so a
duplicate-key re-enqueue that Redis dedup later no-ops STILL bumps ``enqueued``. That drift
is ACCEPTED: ``enqueued`` is a NON-AUTHORITATIVE soft hint only -- the UI renders the
em-dash ``-`` as the real denominator and ``get_stage_progress`` (DB-truth, 35-03) owns
every rendered ``done``. Do NOT add pre-dedup detection to "fix" this.

NOTE -- intent of the ``completed`` counter (plan-checker W4): :func:`increment_completed`
maintains ``phaze:pipeline:completed:<function>`` to satisfy D-02's mandate for MAINTAINED
per-function counters. No node renders it directly (every ``done`` renders from DB-truth
per D-03); it is a deliberate reconcile/backstop cache, NOT dead code. 35-04 documents how
``read_counters`` feeds reconcile-on-read without overriding the DB-truth ``done``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from saq.job import TERMINAL_STATUSES, Status
import structlog

from phaze.services.pipeline_counters import incr_completed, incr_enqueued


if TYPE_CHECKING:
    from collections.abc import Callable

    from saq import Job


logger = structlog.get_logger(__name__)


def _hash_ids(file_ids: Any) -> str:
    """Return an order-independent sha256 hex digest of a batch of ids.

    ``generate_proposals`` is a batch task (35-RESEARCH Q3): its job identity is the
    SET of ``file_ids``, not any single file. Sorting before hashing makes
    ``[A, B, C]``, ``[C, B, A]`` and ``[B, A, C]`` collapse to the SAME key, so a
    re-enqueue of the same batch dedups regardless of caller ordering. Per-file
    idempotency lives in the proposals upsert (35-02), not in this key.
    """
    joined = ",".join(sorted(str(i) for i in file_ids))
    return hashlib.sha256(joined.encode()).hexdigest()


# Exactly 8 entries. Each builder maps a job's kwargs (the task payload) to the natural
# id that makes a re-enqueue of the same logical work dedup. Natural ids VERIFIED present
# in each payload (35-RESEARCH Q1 table). MUST stay in sync with
# ``pipeline_counters.PIPELINE_FUNCTIONS`` and the drift-guard test's routable universe.
_KEY_BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "process_file": lambda k: str(k["file_id"]),
    "extract_file_metadata": lambda k: str(k["file_id"]),
    "fingerprint_file": lambda k: str(k["file_id"]),
    "scan_live_set": lambda k: str(k["file_id"]),
    "search_tracklist": lambda k: str(k["file_id"]),
    "scrape_and_store_tracklist": lambda k: str(k["tracklist_id"]),
    "match_tracklist_to_discogs": lambda k: str(k["tracklist_id"]),
    "generate_proposals": lambda k: _hash_ids(k["file_ids"]),
}


async def apply_deterministic_key(job: Job) -> None:
    """SAQ ``before_enqueue`` hook -- set ``job.key`` deterministically + bump ``enqueued``.

    For a function in :data:`_KEY_BUILDERS`, sets ``job.key`` to
    ``"<function>:<natural_id>"`` UNCONDITIONALLY (overriding any caller-supplied key --
    anti-drift) so SAQ's per-queue ``incomplete``-set dedup collapses a repeat enqueue of
    the same logical work to a no-op. Functions NOT in the registry are left untouched
    (they keep SAQ's random-uuid default key).

    The ``enqueued`` counter INCR is folded in here (one hook does key + counter) and is
    strictly best-effort: the Redis handle is read from ``job.queue.cache_redis`` and any
    failure is logged, never raised -- a counter hiccup must never block an enqueue.
    """
    builder = _KEY_BUILDERS.get(job.function)
    if builder is None:
        return

    job.key = f"{job.function}:{builder(job.kwargs or {})}"

    # Best-effort enqueued counter. Phase 36: the broker is Postgres now, so the cache client
    # is the decoupled ``cache_redis`` handle the factory attaches to the queue object (NOT
    # ``job.queue.redis`` -- PostgresQueue has no such attribute). Degrade silently if it is
    # absent (e.g. a test fake without a wired cache_redis).
    try:
        redis = getattr(job.queue, "cache_redis", None)
        if redis is not None:
            await incr_enqueued(redis, job.function)
    except Exception:
        # Counter is a cache; never block the enqueue on a Redis hiccup.
        logger.warning("pipeline enqueued-counter increment failed", function=job.function, exc_info=True)

    # Phase 45 (L-01): durable scheduling-ledger WRITE at the single before_enqueue chokepoint.
    # The DB handle hangs off the queue (symmetric with cache_redis); it is ONLY present on the
    # control-side queues (controller + per-agent router queues). On the agent worker queue (and
    # test fakes) it is absent, so this whole block degrades to a logged no-op -- the agent
    # boundary stays Postgres-free (T-45-02). The import is function-LOCAL so the module-level
    # graph never pulls phaze.services.scheduling_ledger (and thus phaze.models / sqlalchemy.ext
    # .asyncio) on the agent path -- it executes only when ledger_sessionmaker is present.
    try:
        sm = getattr(job.queue, "ledger_sessionmaker", None)
        if sm is not None:
            # INTENTIONAL function-local import (PLC0415 suppressed on the import line below):
            # this module is _shared (the agent worker imports it); a top-level import of the
            # ledger service would drag phaze.models / sqlalchemy.ext.asyncio into the agent graph
            # and break the Postgres-free boundary (test_task_split). It runs only when a
            # control-side ledger_sessionmaker is present.
            from phaze.services.scheduling_ledger import upsert_ledger_entry  # noqa: PLC0415

            async with sm() as session:
                await upsert_ledger_entry(session, key=job.key, function=job.function, kwargs=dict(job.kwargs or {}))
                await session.commit()
    except Exception:
        # The ledger is best-effort here; a hiccup degrades to "row not written" (recovered by the
        # Plan-04 backfill / next recovery) and must NEVER block an enqueue (T-45-03).
        logger.warning("scheduling-ledger upsert failed", function=job.function, key=job.key, exc_info=True)


async def increment_completed(ctx: dict[str, Any]) -> None:
    """SAQ ``after_process`` hook -- bump ``completed`` on COMPLETE + clear the ledger on terminal.

    Wired as a Worker constructor kwarg (``"after_process"``) in both worker settings dicts
    (35-RESEARCH Q2). ``after_process`` runs in a ``finally`` after EVERY outcome, so
    ``job.status`` is the authoritative terminal/non-terminal signal: ``finish()`` sets a
    terminal status, ``retry()`` sets ``Status.QUEUED``.

    Two best-effort actions, both gated on ``job.function in _KEY_BUILDERS`` and never raising:

    1. completed-counter INCR -- only on ``Status.COMPLETE`` (preserves the Phase-35 contract).
    2. Phase 45 (L-02, controller half) scheduling-ledger CLEAR -- on ``job.status in
       TERMINAL_STATUSES`` {COMPLETE, FAILED, ABORTED}, NOT on a retry (Status.QUEUED). Locked
       decision #1: a terminal ``failed`` clears the row (no poison re-queue) just like success.
       The clear only reaches Postgres when ``ledger_sessionmaker`` is present (controller
       worker); on the agent worker (no handle) it is a logged no-op -- agent-stage clears are
       Plan 02's job (the control-side callback handlers).
    """
    job = ctx.get("job")
    if job is None or job.function not in _KEY_BUILDERS:
        return

    if job.status == Status.COMPLETE:
        try:
            redis = getattr(job.queue, "cache_redis", None)
            if redis is not None:
                await incr_completed(redis, job.function)
        except Exception:
            # Counter is a cache; never block job teardown on a Redis hiccup.
            logger.warning("pipeline completed-counter increment failed", function=job.function, exc_info=True)

    if job.status in TERMINAL_STATUSES:
        # Function-LOCAL import (mirrors the WRITE hook) so the agent import graph stays
        # Postgres-free; it executes only when ledger_sessionmaker is present (control-side).
        try:
            sm = getattr(job.queue, "ledger_sessionmaker", None)
            if sm is not None:
                # INTENTIONAL function-local import (see apply_deterministic_key; PLC0415 suppressed
                # on the import line): keeps the ledger service out of the agent's _shared import
                # graph; runs only when a control-side ledger_sessionmaker is present.
                from phaze.services.scheduling_ledger import clear_ledger_entry  # noqa: PLC0415

                async with sm() as session:
                    await clear_ledger_entry(session, job.key)
                    await session.commit()
        except Exception:
            # Best-effort: a clear hiccup leaves the row for the next recovery; never raise (T-45-03).
            logger.warning("scheduling-ledger clear failed", function=job.function, key=job.key, exc_info=True)


__all__ = ["apply_deterministic_key", "increment_completed"]
