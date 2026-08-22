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
  bound is only correct for jobs whose timeout happens to match it. The motivating case was
  ``process_file``'s then-7200s job timeout: a timed-out one entered ``'aborting'`` at age 7200s --
  already ~8x past the OLD 900s absolute bound -- so the every-minute cron could steal the row from
  a worker still mid-finalization (SAQ's sweeper waits up to ~5s for ``finish(ABORTED)`` to land).
  Since phaze-w55w1 ``process_file`` itself is out of scope for this reaper entirely: it runs
  ``timeout=0``, and ``timeout: 0`` rows are EXCLUDED (see the guard below). The per-row derivation
  is unchanged and still right for every other long job. SAQ's ``Job.to_dict`` always serializes ``timeout`` once it differs
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
A reaper hiccup must never abort a controller cron tick. That handler is deliberately broad;
see :mod:`phaze.tasks._saq_reap` for why narrowing it would break the contract it implements.

SIBLING (phaze-o0n6): ``'aborting'`` is not the only status outside SAQ's overwrite allowlist --
``'active'`` has the identical blocking property and gets the identical three guards in
:mod:`phaze.tasks.active_reaper`. The guards themselves now live ONCE in :mod:`phaze.tasks._saq_reap`
so a fix to one reaper can never silently miss the other; this module supplies only the status and
the slack. Nothing else changed: the statement is byte-equivalent to the literal it replaced, with
``'aborting'`` moved from an inline literal to a bind parameter.
"""

from __future__ import annotations

from typing import Any

import structlog

from phaze.config import get_settings
from phaze.tasks._saq_reap import REAP_STRANDED_SQL, SAQ_DEFAULT_TIMEOUT_SECONDS


logger = structlog.get_logger(__name__)


# The shared stranded-row DELETE (see :mod:`phaze.tasks._saq_reap` for the three guards and why they
# are written once). Aliased under the module-local name this module has always used so the existing
# degrade test can keep monkeypatching it.
_REAP_ABORTING_SQL = REAP_STRANDED_SQL


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
                    {"status": "aborting", "slack_seconds": slack, "default_timeout_seconds": SAQ_DEFAULT_TIMEOUT_SECONDS},
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
