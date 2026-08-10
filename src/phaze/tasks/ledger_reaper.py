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
- **Scoped to rows with a per-file ledger key AND a completion predicate.** That is the two enrich
  stages (via ``resolved_ledger_clause``) plus, since phaze-k95r7, the two CLOUD-LANE functions
  ``push_file`` / ``s3_upload`` (via ``resolved_cloud_ledger_clause``). The controller functions stay
  deliberately untouched: their clear rides the broker's own ``after_process``, so a surviving row
  there is genuinely orphaned, not leaked.

THE CLOUD-LANE PASS (phaze-k95r7). ``push_file`` / ``s3_upload`` are file-keyed AGENT tasks, so they
leak ledger rows for exactly the same reasons the enrich stages do -- but they are not
:class:`~phaze.enums.stage.Stage` members, so no stage-keyed reconciler could ever have named them.
Measured on the live deployment 2026-08-08: 17 ``s3_upload`` rows dated 2026-07-07/14, every file
successfully analyzed and its ``cloud_job`` row long since cleaned up. Nothing reaped them, and because
``s3_upload`` also had no recovery-side completion exclusion, every recovery run handed all 17 to the
regenerator, which could not resolve a bucket (no ``cloud_job`` row to read one from) and reported them
``unreplayable`` -- permanently unreplayable AND permanently counted. They were deleted by hand to clear
the noise; this pass is what stops them coming back. The completion half of the predicate is
``cloud_lane_completed_clause`` -- the SAME builder ``reenqueue._build_done_sets`` derives its
``cloud_lane_done`` exclusion from, so recovery ignoring a row and this reaper clearing it are two
consequences of one definition rather than two definitions that must be kept in step.

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

from sqlalchemy import Select, delete, select
import structlog

from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, Stage
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.stage_status import CLOUD_LANE_FUNCTIONS, ledger_key_for_function, resolved_cloud_ledger_clause, resolved_ledger_clause
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
    return select(ledger_key_for_function(func_name)).where(resolved_ledger_clause(stage))


def _resolved_cloud_keys_subquery(func_name: str) -> Select[tuple[str]]:
    """Return a SELECT of ``func_name``'s CLOUD-LANE ledger keys that are RESOLVED (phaze-k95r7).

    The function-keyed twin of :func:`_resolved_keys_subquery`, identical in shape -- drive off
    ``files``, rebuild the deterministic key with the SAME :func:`ledger_key_for_function` spelling the
    predicate itself uses, and let :func:`resolved_cloud_ledger_clause` supply the correlated
    ``exists`` conjuncts.
    """
    return select(ledger_key_for_function(func_name)).where(resolved_cloud_ledger_clause(func_name))


async def _reap_keys(session: AsyncSession, label: str, keys: Select[tuple[str]]) -> int:
    """Delete the ledger rows named by ``keys``; return the count. Degrade-safe (returns 0 on any error).

    ``label`` names the lane in the degrade log ONLY (a stage value or a keyed-function name); the
    statement itself is built entirely from ORM columns and bound params.
    """
    stmt = delete(SchedulingLedger).where(SchedulingLedger.key.in_(keys))
    try:
        async with session.begin_nested():
            result = await session.execute(stmt)
    except Exception:
        logger.warning("ledger_reap_degraded", lane=label, exc_info=True)
        return 0
    # `session.execute` is typed `Result[Any]`; a DML statement always yields a CursorResult, which is
    # where `rowcount` lives. Same narrowing `services/backends.py` performs on its own DML results.
    return int(type_cast("CursorResult[Any]", result).rowcount or 0)


async def reap_resolved_ledger_rows(ctx: dict[str, Any]) -> dict[str, int]:
    """Clear ``scheduling_ledger`` rows whose work has finished and which are running nowhere.

    Returns ``{"reaped": N}`` plus a per-lane breakdown -- the two enrich stages by stage value
    (``analyze`` / ``metadata``) and the two cloud-lane producers by keyed-function name
    (``push_file`` / ``s3_upload``), each 0 when nothing is stale. Never raises: a degraded probe rolls
    the SAVEPOINT back alone and that lane contributes 0.
    """
    per_lane: dict[str, int] = {}
    async with ctx["async_session"]() as session:
        for stage in _REAPABLE_STAGES:
            per_lane[stage.value] = await _reap_keys(session, stage.value, _resolved_keys_subquery(stage))
        # phaze-k95r7: the cloud-lane pass. Same predicate SHAPE, keyed by function rather than stage,
        # because these producers have a per-file ledger key but no ``Stage`` to hang off.
        for func_name in CLOUD_LANE_FUNCTIONS:
            per_lane[func_name] = await _reap_keys(session, func_name, _resolved_cloud_keys_subquery(func_name))
        await session.commit()

    total = sum(per_lane.values())
    if total:
        # Loud + explicit (the epic's "say so rather than going quiet" rule): these rows had been
        # reporting their files as in-flight, and holding them out of eligibility and the cloud drain.
        logger.warning("resolved scheduling_ledger rows cleared: stale in-flight state reconciled", reaped=total, **per_lane)

    return {"reaped": total, **per_lane}
