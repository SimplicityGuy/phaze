"""Stable control-side facade for ledger-driven pipeline recovery.

Recovery re-enqueues only previously scheduled work that is no longer live, is not domain
complete, and has no other cloud owner. The implementation is separated into pure policy and
planning, database queries, replay/regeneration adapters, and startup backfill. This module keeps
the supported production imports and historical private test/monkeypatch paths stable.

Transaction ownership is deliberate: the read transaction commits before network replay, owner
lookup commits before its enqueue loop, regenerators commit or roll back their own staging effect,
and the startup caller—not the backfill adapter—owns the outer commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from phaze.config import get_settings
from phaze.services import cloud_staging  # noqa: F401 -- compatibility patch path
from phaze.services.pipeline import count_inflight_jobs, get_live_job_keys
from phaze.services.scheduling_ledger import get_ledger_rows
from phaze.tasks.recovery_backfill import (  # noqa: F401 -- compatibility facade
    _BACKFILL_SAQ_JOBS_SQL,
    _classify_saq_job_row,
    _keyed_function_from_job_blob,
    _parse_job_blob,
    _serialized_job_bound,
    backfill_ledger_from_saq_jobs,
)
from phaze.tasks.recovery_policy import (  # noqa: F401 -- compatibility facade
    _ALL_KEYED_FUNCTIONS,
    _CLOUD_OWNED_FUNCTIONS,
    _DOMAIN_COMPLETED_STAGES,
    TALLY_KEYS,
    _DoneSets,
    _has_live_job,
    _is_metadata_domain_completed,
    _is_orphaned,
    _is_owned_by_the_cloud_lane,
    _ledger_fids,
    _natural_id,
    _OwnerGroup,
    _OwnerPlan,
    _plan_owner_groups,
    _plan_replay,
    _RegenTarget,
    _ReplayPlan,
    _zero,
    is_domain_completed,
)
from phaze.tasks.recovery_queries import (  # noqa: F401 -- compatibility facade
    _awaiting_cloud_job_ids,
    _build_done_sets,
    _fids_scope,
    _in_flight_cloud_job_ids,
    _select_cloud_lane_done_ids,
    _select_done_analyze_ids,
)
from phaze.tasks.recovery_replay import (  # noqa: F401 -- compatibility facade
    _REPLAY_REGENERATORS,
    StaleLedgerRow,
    UnreplayableRow,
    _regenerate_row_isolated,
    _regenerate_s3_upload,
    _replay_agent_rows_by_owner,
    _replay_owner_group,
    _replay_row,
    _replay_row_isolated,
)


if TYPE_CHECKING:
    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


async def recover_orphaned_work(ctx: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Detect queue loss and replay each owned orphan with per-row failure isolation."""
    _ = cast("ControlSettings", get_settings())

    async with ctx["async_session"]() as session:
        inflight = await count_inflight_jobs(session)
        detected_loss = inflight == 0
        if not force and not detected_loss:
            logger.info("recover_orphaned_work no-op: queue durable (Phase-36 restart)", inflight=inflight)
            return {"detected_loss": False, "forced": False, "unreplayable": 0, "stages": {}}

        rows = await get_ledger_rows(session)
        live = await get_live_job_keys(session)
        done_sets = await _build_done_sets(session, _ledger_fids(rows))
        in_flight = await _in_flight_cloud_job_ids(session)
        awaiting_cloud = await _awaiting_cloud_job_ids(session)
        orphaned = [row for row in rows if _is_orphaned(row, live=live, done_sets=done_sets, in_flight=in_flight, awaiting_cloud=awaiting_cloud)]

        # Close the read transaction before any network-dependent replay. The controller session
        # uses expire_on_commit=False, so the materialized rows remain safe to inspect.
        await session.commit()

        stages: dict[str, dict[str, int]] = {function: _zero() for function in _ALL_KEYED_FUNCTIONS}
        plan = _plan_replay(orphaned)
        for row in plan.controller_rows:
            await _replay_row_isolated(ctx["queue"], row, stages)
        await _replay_agent_rows_by_owner(session, ctx["task_router"], plan.push_rows, stages, required_kind="fileserver")
        await _replay_agent_rows_by_owner(session, ctx["task_router"], plan.other_agent_rows, stages, required_kind=None)
        for target in plan.regenerated:
            await _regenerate_row_isolated(session, ctx["task_router"], target, stages)

    unreplayable = sum(tally["unreplayable"] for tally in stages.values())
    if unreplayable:
        logger.warning(
            "recover_orphaned_work: some orphaned work was NOT re-enqueued (phaze-71nz). These stages are NOT covered by "
            "this run -- see the per-row warnings above for each row's reason.",
            unreplayable=unreplayable,
            stages=sorted(function for function, tally in stages.items() if tally["unreplayable"]),
        )
    logger.info("recover_orphaned_work complete", detected_loss=detected_loss, forced=force, unreplayable=unreplayable, stages=stages)
    return {"detected_loss": detected_loss, "forced": force, "unreplayable": unreplayable, "stages": stages}
