"""Control-side SAQ cron: clear ``scheduling_ledger`` rows whose work is finished (phaze-2u8v.2).

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). Like :mod:`phaze.tasks.aborting_reaper`
this needs PostgreSQL via ``ctx["async_session"]`` and MUST NEVER be imported or registered by
``phaze.tasks.agent_worker`` or anything under ``phaze.tasks._shared`` (the agent path is deliberately
Postgres-free -- ``tests/shared/core/test_task_split.py`` enforces this).

WHY THIS EXISTS
---------------
A ``scheduling_ledger`` row is written once at the ``before_enqueue`` chokepoint and deleted ONLY by a
terminal-outcome callback (``after_process``, or a control-side agent-stage callback). Until this task
there was NO reconciler anywhere: if that one clear never ran, the row stood forever.

That is not a rare interleaving. Named producers, all live:

- :func:`~phaze.tasks.aborting_reaper.reap_stuck_aborting_jobs` DELETEs the ``saq_jobs`` row to release
  the deterministic key and deliberately leaves the ledger row behind -- so phaze-qmc2's own remedy is
  a producer of this leak.
- ``clear_ledger_entry``'s documented residual window (the ledger upsert and SAQ's ``saq_jobs`` insert
  are two separate transactions).
- A broker truncate / restore-from-backup, and a worker killed between its terminal write and its clear.

A leaked row is not cosmetic. Bare ledger existence IS ``in_flight`` (D-01), so the row reports the
file as in flight forever; it makes the file permanently INELIGIBLE via ``eligible_clause``; and it
permanently disqualifies the file from the cloud drain via ``awaiting_candidate_clause``. Meanwhile
``recover_orphaned_work`` will never touch it, because recovery excludes domain-completed rows -- so
the ONE mechanism that could have noticed is structurally blind to exactly this population. On the live
archive this had accumulated to 176 analyze rows.

WHAT IT DOES
------------
Deletes ledger rows matched by :func:`~phaze.services.stage_status.resolved_ledger_clause` -- the
stage has DOMAIN-COMPLETED (done / force-skipped / terminally failed) AND nothing is running it (no
live ``queued``/``active`` ``saq_jobs`` row for the key, no busy ``cloud_job`` for the file). It is the
clear that the lost terminal callback owed, run late.

THREE GUARDS, all load-bearing:

- **Domain-completion is required.** This is what separates a RESOLVED row from an ORPHANED one. An
  orphaned row (scheduled, running nowhere, no outcome) is genuinely owed work that the ledger is RIGHT
  to hold -- it IS the 2026-06-18 over-enqueue guard, and ``recover_orphaned_work`` is what re-drives
  it. Dropping this conjunct would turn a reconciler into a data-loss bug that silently discards ~2146
  files' worth of owed analyze work and re-opens the incident class D-01 was written to close.
- **Liveness is required to be ABSENT, and it counts BOTH substrates.** ``cloud_job`` busy-ness is
  checked alongside ``saq_jobs`` because compute dispatch has no controller-side broker row at all; on
  ``saq_jobs`` alone every dispatched file would look reapable mid-flight.
- **Scoped to the three enrich stages.** ``resolved_ledger_clause`` is only defined where a per-file
  ledger key and a domain-completion predicate both exist. ``push_file`` / ``s3_upload`` /
  ``scan_live_set`` / the controller functions are deliberately untouched.

IDEMPOTENT by construction: the predicate is a pure function of committed state and the action is a
DELETE of the rows it matched, so a second pass over an unchanged database matches nothing and returns
``{"reaped": 0}``. Re-running is a no-op, never a double-effect.

Degrade-safe: the whole statement runs in a SAVEPOINT; any error (a missing/unreadable ``saq_jobs`` in
a pre-migration env) rolls the nested scope back alone and returns ``reaped=0``. A reaper hiccup must
never abort a controller cron tick. Note the ASYMMETRY with ``clear_ledger_entry``, which falls back to
an UNGUARDED delete when its liveness probe fails: that fallback is safe for a caller that already
knows its own job just went terminal, and is NOT safe here, where the probe IS the evidence. When the
probe is unavailable this reaper does nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast as type_cast

from sqlalchemy import Select, String, cast, delete, func, select
import structlog

from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, Stage
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.stage_status import resolved_ledger_clause
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


# The three enrich stages -- the only ones with BOTH a per-file ledger key (STAGE_TO_FUNCTION) and a
# domain-completion predicate. Derived from the same table ``resolved_ledger_clause`` is defined over,
# never re-listed by hand, so the two can never disagree about scope.
_REAPABLE_STAGES: tuple[Stage, ...] = tuple(sorted(ELIGIBLE_AFTER_FAILURE, key=lambda s: s.value))


def _resolved_keys_subquery(stage: Stage) -> Select[tuple[str]]:
    """Return a SELECT of the ledger keys for ``stage`` that are RESOLVED (finished, running nowhere).

    Correlated ``exists`` clauses inside :func:`resolved_ledger_clause` resolve against
    :class:`~phaze.models.file.FileRecord`, so the driving SELECT is over ``files`` and the deterministic
    key is rebuilt from ``STAGE_TO_FUNCTION`` + ``files.id`` -- the SAME spelling ``inflight_clause``
    uses. Selecting keys (rather than deleting through a join) keeps the DELETE a plain
    ``WHERE key IN (...)``, matching ``pipeline.py``'s existing ledger-delete idiom.
    """
    func_name = STAGE_TO_FUNCTION[stage.value]
    return select(func.concat(func_name + ":", cast(FileRecord.id, String))).where(resolved_ledger_clause(stage))


async def _reap_stage(session: AsyncSession, stage: Stage) -> int:
    """Delete ``stage``'s RESOLVED ledger rows; return the count. Degrade-safe (returns 0 on any error)."""
    stmt = delete(SchedulingLedger).where(SchedulingLedger.key.in_(_resolved_keys_subquery(stage)))
    try:
        async with session.begin_nested():
            result = await session.execute(stmt)
    except Exception:
        logger.warning("ledger_reap_degraded", stage=stage.value, exc_info=True)
        return 0
    # `session.execute` is typed `Result[Any]`; a DML statement always yields a CursorResult, which is
    # where `rowcount` lives. Same narrowing `services/backends.py` performs on its own DML results.
    return int(type_cast("CursorResult[Any]", result).rowcount or 0)


async def reap_resolved_ledger_rows(ctx: dict[str, Any]) -> dict[str, int]:
    """Clear ``scheduling_ledger`` rows whose stage has finished and which are running nowhere.

    Returns ``{"reaped": N}`` plus a per-stage breakdown (0 when nothing is stale). Never raises: a
    degraded probe rolls the SAVEPOINT back alone and that stage contributes 0.
    """
    per_stage: dict[str, int] = {}
    async with ctx["async_session"]() as session:
        for stage in _REAPABLE_STAGES:
            per_stage[stage.value] = await _reap_stage(session, stage)
        await session.commit()

    total = sum(per_stage.values())
    if total:
        # Loud + explicit (the epic's "say so rather than going quiet" rule): these rows had been
        # reporting their files as in-flight, and holding them out of eligibility and the cloud drain.
        logger.warning("resolved scheduling_ledger rows cleared: stale in-flight state reconciled", reaped=total, **per_stage)

    return {"reaped": total, **per_stage}
