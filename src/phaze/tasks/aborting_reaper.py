"""Control-side SAQ cron: reap job rows stuck forever in ``status='aborting'`` (phaze-e57w).

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). Like :mod:`phaze.tasks.scan_reaper`
this needs PostgreSQL via ``ctx["async_session"]`` and MUST NEVER be imported or registered by
``phaze.tasks.agent_worker`` or anything under ``phaze.tasks._shared`` (the agent path is
deliberately Postgres-free -- ``tests/shared/core/test_task_split.py`` enforces this).

WHY THIS EXISTS
---------------
SAQ's sweeper aborts a stuck job by moving it to ``status='aborting'`` and then waiting for the
worker that owns it to finalize the abort (``finish(ABORTED)``). When that worker is gone -- or
never actually ran the job (a claimed-but-buffered row, spike phaze-qmc2.1) -- NOTHING ever
completes the ``aborting -> aborted`` transition and the row sticks in ``aborting`` forever.

That zombie is not merely cosmetic. ``apply_deterministic_key`` stamps every fingerprint job
``key = 'fingerprint_file:<file_id>'``, and SAQ's ``_enqueue`` upsert only overwrites a conflicting
key whose status is in ``('aborted','complete','failed')`` (saq/queue/postgres.py). ``'aborting'``
is NOT in that allowlist, so while the zombie holds the key EVERY re-enqueue of that file collapses
to a ``None`` return -- the file is silently un-requeueable by any path, including the recovery CLI
(phaze-e57w observation, confirmed at the SQL level in spike phaze-qmc2.1).

WHAT IT DOES
------------
Deletes rows in ``status='aborting'`` whose frozen ``started`` timestamp is older than
``aborting_reap_seconds`` (default 600s timeout + 300s slack). DELETE (rather than a move to
``aborted``) is the cleanest release: it removes the key entirely, so the next enqueue INSERTs
fresh. This mirrors the manual remediation Robert approved on 2026-07-19 (DELETE scoped by exact
key AND ``status='aborting'`` in a transaction).

TWO GUARDS, both load-bearing:

- **Age bound on ``started``, NOT ``touched``.** SAQ's sweeper bumps ``touched`` on every
  ``abort -> aborting`` pass (``Queue.update`` sets ``touched = now()``), so a touched-based bound
  would never fire on a repeatedly-swept zombie. ``started`` is frozen at (last) dequeue/process
  start, so ``now - started`` is the true age of the stuck state (spike phaze-qmc2.1, fact 4).
- **CAS on ``status='aborting'`` in the DELETE's WHERE.** A job that is genuinely mid-abort (a live
  worker about to call ``finish(ABORTED)``) must not be stolen. Under READ COMMITTED the DELETE
  re-checks the ``status='aborting'`` qualification after locking a concurrently-updated row, so a
  row that flips to ``aborted`` first wins the race and is left alone.

Degrade-safe: the whole statement runs in a SAVEPOINT; a missing/unreadable ``saq_jobs`` table (a
pre-migration env, or a malformed blob) rolls the nested scope back alone and returns ``reaped=0``.
A reaper hiccup must never abort a controller cron tick.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
import structlog

from phaze.config import get_settings


logger = structlog.get_logger(__name__)


# Read+DELETE in ONE atomic statement. The ``status = 'aborting'`` predicate IS the CAS (a
# concurrent flip to 'aborted' loses the row from this DELETE under READ COMMITTED re-check). Age
# is computed from the blob's frozen ``started`` (ms) vs now; ``touched`` is deliberately NOT used
# (sweeper-bumped). Rows whose blob lacks ``started`` (shouldn't happen for an active-then-aborting
# job) are excluded rather than reaped on incomplete data.
_REAP_ABORTING_SQL = text(
    """
    DELETE FROM saq_jobs
    WHERE status = 'aborting'
      AND (convert_from(job, 'UTF8')::jsonb ? 'started')
      AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (convert_from(job, 'UTF8')::jsonb->>'started')::bigint)
          / 1000.0 > :bound_seconds
    RETURNING key
    """
)


async def reap_stuck_aborting_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Delete ``saq_jobs`` rows stuck in ``status='aborting'`` past ``aborting_reap_seconds``.

    Releases each reaped row's deterministic key so the underlying file becomes re-queueable
    through the normal recovery path. Returns ``{"reaped": N}`` (0 when nothing is stuck). Never
    raises: a degraded read/parse rolls back the SAVEPOINT alone and returns ``reaped=0``.
    """
    bound = get_settings().aborting_reap_seconds

    async with ctx["async_session"]() as session:
        try:
            async with session.begin_nested():
                result = await session.execute(_REAP_ABORTING_SQL, {"bound_seconds": bound})
                reaped_keys = [row[0] for row in result.fetchall()]
        except Exception:
            logger.warning("aborting_reap_degraded: saq_jobs delete failed (pre-migration env?)", exc_info=True)
            return {"reaped": 0}
        await session.commit()

    if reaped_keys:
        # Loud + explicit: name every freed key so an operator can see exactly which files were
        # un-blocked (the epic's "say so rather than going quiet" rule). These files are now
        # eligible for the normal recovery path again.
        logger.warning(
            "aborting zombies reaped: deterministic keys released",
            reaped=len(reaped_keys),
            bound_seconds=bound,
            keys=reaped_keys,
        )

    return {"reaped": len(reaped_keys)}
