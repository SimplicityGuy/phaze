"""Transfer abandoned local ownership to the durable awaiting-cloud obligation.

This is deliberately separate from resolved-ledger cleanup: the analysis is still
owed. Only an awaiting sidecar authorizes removing its stale local scheduling row.
The dispatcher remains the sole submitter; this pass never enqueues or spends budgets.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import ProgrammingError
import structlog

from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.analysis_attempt import try_lock_analysis_attempt
from phaze.services.route_control import get_route_control
from phaze.services.stage_status import awaiting_candidate_clause, domain_completed_clause, ledger_key_for_function, live_job_clause


if TYPE_CHECKING:
    from datetime import datetime
    import uuid

    from sqlalchemy import CursorResult, Select
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)
_STALE_AFTER = timedelta(days=1)
_BATCH_SIZE = 500


def _conflicts() -> Select[str, datetime, datetime, uuid.UUID]:
    return (
        select(SchedulingLedger.key, SchedulingLedger.enqueued_at, SchedulingLedger.updated_at, CloudJob.id)
        .select_from(FileRecord)
        .join(CloudJob, CloudJob.file_id == FileRecord.id)
        .join(SchedulingLedger, SchedulingLedger.key == ledger_key_for_function("process_file"))
        .where(
            CloudJob.status == CloudJobStatus.AWAITING.value,
            SchedulingLedger.function == "process_file",
            ~domain_completed_clause(Stage.ANALYZE),
        )
    )


async def has_stale_awaiting_ownership(session: AsyncSession) -> bool:
    """Cheap preflight: keep a healthy admission tick at one lock and one commit.

    No broker read here: a before-enqueue ledger/broker interval must still select
    the guarded transfer pass. That pass proves liveness under the attempt lock.
    """
    cutoff = func.clock_timestamp() - _STALE_AFTER
    return bool(
        await session.scalar(select(_conflicts().where(SchedulingLedger.enqueued_at < cutoff, SchedulingLedger.updated_at < cutoff).exists()))
    )


async def cloud_candidate_available(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Recheck candidacy under the attempt lock, including ledger-less broker work.

    Only a missing broker table proves that no broker job exists. Preserve that
    pre-initialization case without treating an unreadable existing broker as empty.
    The lock excludes a producer initializing the table until cloud ownership commits.
    """
    candidate = select(FileRecord.id).join(CloudJob, CloudJob.file_id == FileRecord.id).where(FileRecord.id == file_id, awaiting_candidate_clause())
    try:
        async with session.begin_nested():
            return bool(await session.scalar(candidate.where(~live_job_clause(Stage.ANALYZE))))
    except ProgrammingError as exc:
        if getattr(exc.orig, "sqlstate", None) != "42P01":
            raise
        return bool(await session.scalar(candidate))


async def transfer_awaiting_ownership(session: AsyncSession) -> dict[str, int]:
    """Reconcile one bounded batch under the caller's dispatcher lock; never commit.

    Snapshot generations before acquiring locks, then CAS all observed clocks after
    acquiring the producer lock. A producer holding even an OLD ledger in the interval
    before broker insert wins. Sidecar row locks exclude callbacks and retain the owed
    work through commit; cancellation/rollback restores every removed ledger row.
    """
    cutoff = await session.scalar(select(func.clock_timestamp() - _STALE_AFTER))
    conflicts = _conflicts()
    stale = conflicts.where(SchedulingLedger.enqueued_at < cutoff, SchedulingLedger.updated_at < cutoff, ~live_job_clause(Stage.ANALYZE))
    # Normal local analysis can retain an inert awaiting sidecar. Count only stale,
    # broker-absent overlaps as blocked work, rather than warning about healthy jobs.
    total = int(await session.scalar(select(func.count()).select_from(stale.subquery())) or 0)
    candidates = (await session.execute(stale.order_by(SchedulingLedger.enqueued_at, SchedulingLedger.key).limit(_BATCH_SIZE))).all()
    counts = {"conflicts": total, "transferred": 0, "held_enqueue": 0, "held_changed": 0}
    for key, epoch, updated_at, cloud_id in candidates:
        if not await try_lock_analysis_attempt(session, key):
            counts["held_enqueue"] += 1
            continue
        owner = await session.scalar(
            select(CloudJob.id).where(CloudJob.id == cloud_id, CloudJob.status == CloudJobStatus.AWAITING.value).with_for_update(skip_locked=True)
        )
        if owner is None:
            counts["held_changed"] += 1
            continue
        # A new READ COMMITTED statement after both locks, never the stale snapshot.
        safe_keys = conflicts.with_only_columns(SchedulingLedger.key).where(
            SchedulingLedger.key == key,
            SchedulingLedger.enqueued_at < cutoff,
            SchedulingLedger.updated_at < cutoff,
            ~live_job_clause(Stage.ANALYZE),
        )
        # Keep the generation CAS on the DELETE target itself. PostgreSQL rechecks
        # these columns after waiting on a concurrent row UPDATE; an IN-subquery
        # alone can have materialized the key before that wait.
        result = await session.execute(
            delete(SchedulingLedger).where(
                SchedulingLedger.key.in_(safe_keys),
                SchedulingLedger.enqueued_at == epoch,
                SchedulingLedger.updated_at == updated_at,
            )
        )
        removed = int(cast("CursorResult[Any]", result).rowcount or 0)
        counts["transferred"] += removed
        counts["held_changed"] += 1 - removed
    return counts


async def reconcile_awaiting_ownership(ctx: dict[str, Any]) -> dict[str, int]:
    """Commit only the ownership transfer, before the drain opens its admission unit.

    Ending this transaction before dispatch also lets the local fallback enqueue acquire
    its normal producer lock. A fresh enqueue between units restores the ledger and is
    excluded by the unchanged canonical awaiting candidate predicate.
    """
    from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY  # noqa: PLC0415

    try:
        async with ctx["async_session"]() as session:
            if await get_route_control(session):
                return {"conflicts": 0, "transferred": 0, "held_enqueue": 0, "held_changed": 0}
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
            counts = await transfer_awaiting_ownership(session)
            await session.commit()
    except Exception:
        logger.warning("awaiting ownership reconciliation unavailable; ownership preserved", exc_info=True)
        return {"conflicts": 0, "transferred": 0, "held_enqueue": 0, "held_changed": 0, "degraded": 1}
    if counts["conflicts"]:
        logger.warning("awaiting cloud work blocked by local scheduling ownership", **counts)
    return counts
