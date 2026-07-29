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

That zombie is not merely cosmetic. ``apply_deterministic_key`` stamps every file-keyed job
``key = '<function>:<file_id>'`` (e.g. ``process_file:<file_id>``), and SAQ's ``_enqueue`` upsert only overwrites a conflicting
key whose status is in ``('aborted','complete','failed')`` (saq/queue/postgres.py). ``'aborting'``
is NOT in that allowlist, so while the zombie holds the key EVERY re-enqueue of that file collapses
to a ``None`` return -- the file is silently un-requeueable by any path, including the recovery CLI
(phaze-e57w observation, confirmed at the SQL level in spike phaze-qmc2.1).

WHAT IT DOES
------------
Deletes rows in ``status='aborting'`` whose frozen ``started`` timestamp is older than that ROW's
OWN job timeout plus ``aborting_reap_slack_seconds`` (default 300s). DELETE (rather than a move to
``aborted``) is the cleanest release: it removes the key entirely, so the next enqueue INSERTs
fresh. This mirrors the manual remediation Robert approved on 2026-07-19 (DELETE scoped by exact
key AND ``status='aborting'`` in a transaction).

THREE GUARDS, all load-bearing:

- **Age bound on ``started``, NOT ``touched``.** SAQ's sweeper bumps ``touched`` on every
  ``abort -> aborting`` pass (``Queue.update`` sets ``touched = now()``), so a touched-based bound
  would never fire on a repeatedly-swept zombie. ``started`` is frozen at (last) dequeue/process
  start, so ``now - started`` is the true age of the stuck state (spike phaze-qmc2.1, fact 4).
- **Bound is PER-ROW, derived from that row's own ``timeout``, not a single fixed constant**
  (phaze-lqkz). The predicate measures the ATTEMPT'S RUNTIME (age since ``started``), so a fixed
  bound is only correct for jobs whose timeout happens to match it. ``process_file`` runs with a
  7200s job timeout (``services/analysis_enqueue.py``); a timed-out ``process_file`` enters
  ``'aborting'`` at age 7200s -- already ~8x past the OLD 900s absolute bound -- so the every-minute
  cron could steal the row from a worker still mid-finalization (SAQ's sweeper waits up to ~5s for
  ``finish(ABORTED)`` to land). SAQ's ``Job.to_dict`` always serializes ``timeout`` once it differs
  from the SAQ dataclass default (10s) -- true for every phaze producer via
  ``apply_project_job_defaults`` -- so the row's OWN blob already carries the bound needed; no side
  table or extra state is required. A row whose blob lacks ``timeout`` (a bare/legacy row) falls
  back to the literal SAQ default (10s) plus the slack.
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


# SAQ's Job dataclass default timeout (saq/job.py) -- the fallback bound for a row whose blob has
# no explicit ``timeout`` key (mirrors ``phaze.tasks._shared.queue_defaults._SAQ_DEFAULT_TIMEOUT``,
# that module's single source of truth; duplicated here as a plain int so this SQL-bound module
# stays free of a cross-import for one constant).
_SAQ_DEFAULT_TIMEOUT_SECONDS = 10

# Read+DELETE in ONE atomic statement. The ``status = 'aborting'`` predicate IS the CAS (a
# concurrent flip to 'aborted' loses the row from this DELETE under READ COMMITTED re-check). Age
# is computed from the blob's frozen ``started`` (ms) vs now; ``touched`` is deliberately NOT used
# (sweeper-bumped). Rows whose blob lacks ``started`` (shouldn't happen for an active-then-aborting
# job) are excluded rather than reaped on incomplete data. phaze-lqkz: the bound is now PER-ROW --
# the row's own serialized ``timeout`` (falling back to the bare SAQ default when absent) plus a
# flat slack -- instead of one fixed constant compared against every row regardless of its job's
# own timeout.
_REAP_ABORTING_SQL = text(
    """
    DELETE FROM saq_jobs
    WHERE status = 'aborting'
      AND (convert_from(job, 'UTF8')::jsonb ? 'started')
      AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (convert_from(job, 'UTF8')::jsonb->>'started')::bigint)
          / 1000.0 > COALESCE((convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint, :default_timeout_seconds) + :slack_seconds
    RETURNING key
    """
)


async def reap_stuck_aborting_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Delete ``saq_jobs`` rows stuck in ``status='aborting'`` past their OWN job timeout + slack.

    Releases each reaped row's deterministic key so the underlying file becomes re-queueable
    through the normal recovery path. Returns ``{"reaped": N}`` (0 when nothing is stuck). Never
    raises: a degraded read/parse rolls back the SAVEPOINT alone and returns ``reaped=0``.
    """
    slack = get_settings().aborting_reap_slack_seconds

    async with ctx["async_session"]() as session:
        try:
            async with session.begin_nested():
                result = await session.execute(
                    _REAP_ABORTING_SQL,
                    {"slack_seconds": slack, "default_timeout_seconds": _SAQ_DEFAULT_TIMEOUT_SECONDS},
                )
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
            slack_seconds=slack,
            keys=reaped_keys,
        )

    return {"reaped": len(reaped_keys)}
