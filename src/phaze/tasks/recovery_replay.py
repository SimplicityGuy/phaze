"""Queue, owner-routing, and regeneration adapters for recovery replay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import structlog

from phaze.models.file import FileRecord
from phaze.services import cloud_staging
from phaze.services.cloud_staging import NoCloudJobToRedriveError
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, select_agents_by_ids
from phaze.services.s3_staging import S3StagingError
from phaze.tasks._shared.replay_safety import LEDGER_REPLAY_REGENERATED, find_time_limited_paths
from phaze.tasks.recovery_policy import _OwnerGroup, _plan_owner_groups, _RegenTarget, _zero


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent
    from phaze.models.scheduling_ledger import SchedulingLedger


logger = structlog.get_logger("phaze.tasks.reenqueue")


async def _replay_row(queue: Any, row: SchedulingLedger, tally: dict[str, int]) -> None:
    """Replay one time-invariant ledger payload through its keyed producer."""
    violations = find_time_limited_paths(row.payload)
    if violations:
        logger.error(
            "recover_orphaned_work: REFUSING to replay a ledger payload carrying time-limited material -- "
            "a scheduling_ledger payload must be replayable at an arbitrary future time (phaze-71nz). "
            "Replaying it would enqueue a job guaranteed to fail at the credential check. Declare this "
            "producer in replay_safety.LEDGER_REPLAY_REGENERATED and register a regenerator in "
            "reenqueue._REPLAY_REGENERATORS, or stop storing the derived material in the payload.",
            key=row.key,
            function=row.function,
            payload_paths=violations,
        )
        tally["unreplayable"] += 1
        return

    policy: dict[str, Any] = {}
    if row.timeout is not None:
        policy["timeout"] = row.timeout
    if row.retries is not None:
        policy["retries"] = row.retries
    await queue.connect()
    job = await queue.enqueue(row.function, key=row.key, **policy, **(row.payload or {}))
    if job is None:
        tally["skipped"] += 1
    else:
        tally["reenqueued"] += 1


async def _replay_row_isolated(queue: Any, row: SchedulingLedger, stages: dict[str, dict[str, int]]) -> None:
    """Replay one row while containing any failure to that row's stage tally."""
    tally = stages.setdefault(row.function, _zero())
    try:
        await _replay_row(queue, row, tally)
    except Exception:
        logger.exception(
            "recover_orphaned_work: row replay failed -- row skipped this run, ledger entry remains for the next pass",
            key=row.key,
            function=row.function,
        )
        tally["errored"] += 1


class UnreplayableRow(Exception):
    """A ledger row whose durable regeneration inputs are unavailable right now."""


class StaleLedgerRow(UnreplayableRow):
    """A ledger row that describes work which is no longer pending."""


async def _regenerate_s3_upload(session: AsyncSession, task_router: Any, target: _RegenTarget, tally: dict[str, int]) -> None:
    """Re-drive an orphaned upload through the live producer with fresh credentials."""
    raw_fid = target.payload.get("file_id")
    if raw_fid is None:
        raise UnreplayableRow("s3_upload ledger row carries no payload['file_id'] -- nothing durable to regenerate from")
    try:
        file_uuid = uuid.UUID(str(raw_fid))
    except ValueError as exc:
        raise UnreplayableRow(f"s3_upload ledger row carries a non-UUID file_id {raw_fid!r}") from exc
    fid = str(raw_fid)

    file = await session.get(FileRecord, file_uuid)
    if file is None:
        raise UnreplayableRow(f"file {fid} no longer exists -- the staged upload has no source to re-presign")

    try:
        await cloud_staging.redrive_upload(session, file, task_router)
        await session.commit()
    except NoCloudJobToRedriveError as exc:
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise StaleLedgerRow(str(exc)) from exc
    except (NoActiveAgentError, S3StagingError) as exc:
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise UnreplayableRow(str(exc)) from exc
    except BaseException:
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise

    fired = await cloud_staging.flush_pending_s3_enqueues(session)
    if fired:
        tally["reenqueued"] += 1
    else:
        tally["skipped"] += 1


_REPLAY_REGENERATORS: dict[str, Callable[[AsyncSession, Any, _RegenTarget, dict[str, int]], Awaitable[None]]] = {
    "s3_upload": _regenerate_s3_upload,
}

if set(_REPLAY_REGENERATORS) != LEDGER_REPLAY_REGENERATED:
    raise RuntimeError(
        "replay-safety drift (phaze-71nz): reenqueue._REPLAY_REGENERATORS "
        f"{sorted(_REPLAY_REGENERATORS)} != replay_safety.LEDGER_REPLAY_REGENERATED {sorted(LEDGER_REPLAY_REGENERATED)}"
    )


async def _regenerate_row_isolated(
    session: AsyncSession,
    task_router: Any,
    target: _RegenTarget,
    stages: dict[str, dict[str, int]],
) -> None:
    """Regenerate one row while preserving the distinct skip/error tally outcomes."""
    tally = stages.setdefault(target.function, _zero())
    regenerator = _REPLAY_REGENERATORS[target.function]
    try:
        await regenerator(session, task_router, target, tally)
    except StaleLedgerRow as exc:
        logger.warning(
            "recover_orphaned_work: row NOT replayed -- it points at work that is not pending, so there is nothing to "
            "regenerate (this is NOT a time-limited/expired payload; the ledger entry is stale and the reaper clears it "
            "once the work reads domain-complete)",
            key=target.key,
            function=target.function,
            reason=str(exc),
        )
        tally["unreplayable"] += 1
    except UnreplayableRow as exc:
        logger.warning(
            "recover_orphaned_work: row NOT replayed -- its payload is time-limited and cannot be regenerated right now "
            "(skipped deliberately, never replayed with stale credentials; ledger entry remains for the next pass)",
            key=target.key,
            function=target.function,
            reason=str(exc),
        )
        tally["unreplayable"] += 1
    except Exception:
        logger.exception(
            "recover_orphaned_work: row regeneration failed -- row skipped this run, ledger entry remains for the next pass",
            key=target.key,
            function=target.function,
        )
        tally["errored"] += 1


async def _replay_owner_group(task_router: Any, agent: Agent, group: _OwnerGroup, stages: dict[str, dict[str, int]]) -> None:
    """Replay one resolved owner's rows onto that owner's per-function queues."""
    for row in group.rows:
        try:
            agent_queue = task_router.queue_for(agent.id, lane_for_task(row.function))
        except Exception:
            logger.exception(
                "recover_orphaned_work: agent row lane routing failed -- row skipped this run, ledger entry remains for the next pass",
                key=row.key,
                function=row.function,
                agent_id=group.owner_id,
            )
            stages.setdefault(row.function, _zero())["errored"] += 1
            continue
        await _replay_row_isolated(agent_queue, row, stages)


async def _replay_agent_rows_by_owner(
    session: AsyncSession,
    task_router: Any,
    rows: Sequence[SchedulingLedger],
    stages: dict[str, dict[str, int]],
    *,
    required_kind: str | None,
) -> None:
    """Resolve and replay agent rows by their stored owner, never by another live agent."""
    plan = _plan_owner_groups(rows)
    if plan.ownerless:
        logger.warning(
            "recover_orphaned_work: agent-routed ledger row has no owning agent_id -- skipped, never rerouted (phaze-fjii)",
            functions=sorted({row.function for row in plan.ownerless}),
            rows=len(plan.ownerless),
        )
    if not plan.groups:
        return

    owners = await select_agents_by_ids(session, [group.owner_id for group in plan.groups], kind=required_kind)
    await session.commit()
    for group in plan.groups:
        agent = owners.get(group.owner_id)
        if agent is None:
            logger.warning(
                "recover_orphaned_work: owning agent offline -- rows skipped, not rerouted (phaze-fjii)",
                agent_id=group.owner_id,
                required_kind=required_kind,
                functions=sorted({row.function for row in group.rows}),
                rows=len(group.rows),
            )
            continue
        await _replay_owner_group(task_router, agent, group, stages)
