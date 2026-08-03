"""Control-side SAQ cron: reap job rows stranded forever in ``status='active'`` (phaze-o0n6).

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). Like :mod:`phaze.tasks.aborting_reaper`
this needs PostgreSQL via ``ctx["async_session"]`` and MUST NEVER be imported or registered by
``phaze.tasks.agent_worker`` or anything under ``phaze.tasks._shared`` (the agent path is deliberately
Postgres-free -- ``tests/shared/core/test_task_split.py`` enforces this).

WHY THIS EXISTS
---------------
The SIBLING of :mod:`phaze.tasks.aborting_reaper`, for the OTHER status that holds a deterministic key
hostage. SAQ's ``_enqueue`` upsert only overwrites a conflicting key whose status is in
``('aborted','complete','failed')`` (``saq/queue/postgres.py``). ``'aborting'`` is not in that
allowlist -- that is phaze-e57w. **``'active'`` is not in it either**, has the identical blocking
property, and had no reaper at all.

``PostgresQueue._dequeue`` runs a bulk ``UPDATE saq_jobs SET status='active' ... LIMIT %(limit)s`` and
pushes the returned rows into an IN-MEMORY ``asyncio.Queue`` that only ``concurrency`` worker
``process()`` tasks drain. The database records ``active`` at CLAIM time; execution happens later, and
possibly never. Any process death in that window -- restart, deploy, OOM, kill -- abandons every
buffered row as ``active`` with nothing alive to finalize it (the "claimed-but-buffered row" of spike
phaze-qmc2.1, quantified by :mod:`phaze.services.queue_introspection`).

Measured on the live deployment 2026-07-31: **2,413** rows stranded ``active`` on one analyze queue
against worker concurrency **4**; **2,411** of the files they keyed had never been analyzed and could
not be re-enqueued by ANY path, including the recovery CLI. The population accumulated in bursts over
six days and did not self-heal: SAQ's sweeper managed 23 passes in a whole worker lifetime, 22 of them
ending in ``Could not abort`` (a per-job ``asyncio.TimeoutError`` wait, processed serially), and the
count held static across a 60-second sample.

That last point CORRECTS the optimistic note in :mod:`phaze.services.queue_introspection`'s docstring
("a claimed-but-unrun row is NOT lost: once it passes ``timeout`` the sweep aborts it"). The sweep is
the theoretical remedy; in the field it does not keep up, and there is no ceiling on the backlog it
falls behind by.

WHAT IT DOES
------------
Deletes rows in ``status='active'`` whose FROZEN ``started`` timestamp is older than that ROW's OWN job
timeout plus :attr:`~phaze.config.BaseSettings.active_reap_slack_seconds`, using the shared guards in
:mod:`phaze.tasks._saq_reap` (read that module -- it is where the three guards and their rationale
live).

DELETE, NOT A TRANSITION TO A TERMINAL STATUS (acceptance item 2). Both options release the key, but
deletion is the established precedent (``aborting_reaper``, and the manual remediation Robert approved
2026-07-19) and it is the STRICTLY safer of the two here:

- A DELETE vacates the key, so the next enqueue is a plain INSERT. A transition to ``'aborted'`` leaves
  the row in place and makes the next enqueue take SAQ's ``ON CONFLICT ... DO UPDATE`` branch, which
  carries a SECOND predicate: ``AND %(scheduled)s > saq_jobs.scheduled``. A replay's ``scheduled``
  defaults to ``now_seconds()`` and so usually clears that -- but NOT for a row whose own ``scheduled``
  sits in the FUTURE, which is exactly what SAQ's retry backoff writes. Such a row would silently
  no-op AGAIN, re-creating the "returns None, file is un-requeueable" symptom this bead exists to
  remove, from a row that now LOOKS reaped. Verified against the real broker in
  ``tests/integration/test_pg_active_reap.py::test_terminal_status_is_not_as_strong_a_release_as_delete``.
- A row claimed-but-never-run (``started == touched``, no ``attempts``) never produced an outcome, so
  there is no result to preserve by keeping it; a terminal status would assert an outcome that never
  happened.

THE LEDGER ROW IS DELIBERATELY LEFT BEHIND (acceptance item 3 -- the bead's OPEN QUESTION).
Every stranded file also carries a ``scheduling_ledger`` row keyed ``process_file:<file_id>``. That row
is the RECOVERY SOURCE, not a second block, and deleting it would destroy the ability to recover the
file. ``recover_orphaned_work`` re-enqueues exactly

    orphaned = (ledger rows) MINUS (live saq_jobs keys) MINUS (domain-completed)

so while the stranded ``active`` row exists the key is LIVE and the ledger row is invisible to
recovery; the moment this reaper deletes it the SAME ledger row becomes an orphan and recovery replays
its stored payload (with its stored 7200s timeout) through the original keyed producer. Releasing the
SAQ key is therefore not merely necessary but SUFFICIENT -- provided the ledger row survives. Verified
end-to-end against a test database in
``tests/analyze/tasks/test_active_reaper.py::test_reaped_active_row_makes_its_ledger_row_recoverable``,
which also pins the counterfactual: delete the ledger row too and recovery re-enqueues NOTHING, exactly
the data-loss shape :mod:`phaze.tasks.ledger_reaper` warns about ("an orphaned row is genuinely owed
work that the ledger is RIGHT to hold"). ``reap_resolved_ledger_rows`` still clears the ledger row
later IF the file turns out to be domain-complete, which is the correct division of labour: this reaper
owns the broker key, that one owns the finished-work ledger.

WHY A WIDER SLACK THAN THE 'aborting' REAPER (900s vs 300s).
``'aborting'`` is a POST-give-up status: SAQ has already decided the job is dead. ``'active'`` is a LIVE
status, so the cost of being wrong is higher, and there is one plausible way to be wrong that
``'aborting'`` does not have: ``process_file`` spends its time inside essentia's C extension, which does
not yield to the event loop, so ``asyncio.wait_for``'s cancellation at the job's own timeout cannot land
until the native call returns. A genuinely-running job can therefore outlive its own timeout by a
bounded margin. The slack is the margin, and it is additive on top of the row's own timeout (a 7200s
``process_file`` gets 8100s, not 900s). Being wrong is not data loss either way -- the worst case is a
duplicate analysis of one file -- but the wider default keeps that from being routine.

Degrade-safe: the whole statement runs in a SAVEPOINT; a missing/unreadable ``saq_jobs`` table (a
pre-migration env, or a malformed blob) rolls the nested scope back alone and returns ``reaped=0``. A
reaper hiccup must never abort a controller cron tick.
"""

from __future__ import annotations

from typing import Any

import structlog

from phaze.config import get_settings
from phaze.tasks._saq_reap import REAP_STRANDED_SQL, SAQ_DEFAULT_TIMEOUT_SECONDS


logger = structlog.get_logger(__name__)


# The shared stranded-row DELETE, bound to ``'active'`` (see :mod:`phaze.tasks._saq_reap`). Held under
# a module-local name so a test can monkeypatch THIS reaper's statement without touching the sibling's.
_REAP_ACTIVE_SQL = REAP_STRANDED_SQL


async def reap_stranded_active_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Delete ``saq_jobs`` rows stranded in ``status='active'`` past their OWN job timeout + slack.

    Releases each reaped row's deterministic key. The file's ``scheduling_ledger`` row is deliberately
    LEFT IN PLACE -- it is what ``recover_orphaned_work`` replays once the key is free (see the module
    docstring). Returns ``{"reaped": N}`` (0 when nothing is stranded). Never raises: a degraded
    read/parse rolls back the SAVEPOINT alone and returns ``reaped=0``.
    """
    slack = get_settings().active_reap_slack_seconds

    async with ctx["async_session"]() as session:
        try:
            async with session.begin_nested():
                result = await session.execute(
                    _REAP_ACTIVE_SQL,
                    {"status": "active", "slack_seconds": slack, "default_timeout_seconds": SAQ_DEFAULT_TIMEOUT_SECONDS},
                )
                reaped_keys = [row[0] for row in result.fetchall()]
        except Exception:
            logger.warning("active_reap_degraded: saq_jobs delete failed (pre-migration env?)", exc_info=True)
            return {"reaped": 0}
        await session.commit()

    if reaped_keys:
        # Loud + explicit: name every freed key so an operator can see exactly which files were
        # un-blocked (the epic's "say so rather than going quiet" rule). Each of these files now has an
        # ORPHANED ledger row and is eligible for the normal recovery path again.
        logger.warning(
            "stranded 'active' jobs reaped: deterministic keys released (ledger rows kept -- they are the recovery source)",
            reaped=len(reaped_keys),
            slack_seconds=slack,
            keys=reaped_keys,
        )

    return {"reaped": len(reaped_keys)}
