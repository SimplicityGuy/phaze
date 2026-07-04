"""Control-side bounded cloud-window staging: top the ≤N window up from AWAITING_CLOUD (Phase 50).

THE SINGLE "STAY ONE AHEAD" DRIVER (CLOUDPIPE-01/-05). Phase 49's per-file router HOLDS every
cloud-routed long file in ``FileState.AWAITING_CLOUD`` (it enqueues NOTHING to compute -- that
direct path was removed in Phase 50 so the window cannot be bypassed). This cron is the ONLY thing
that introduces new push work: every ~5 min it tops the in-flight window up to
``cloud_max_in_flight`` by staging ``push_file`` for the oldest held files. Registered as a SINGLE
narrow ``CronJob(stage_cloud_window, "*/5 * * * *")`` on the controller, REPLACING the deprecated
Phase-49 ``release_awaiting_cloud`` drain cron (which drained the WHOLE held set straight to
process_file -- unbounded, and incompatible with the bounded push pipeline).

Window math (RESEARCH §"Stay one ahead"): ``window = COUNT(state IN {PUSHING, PUSHED})`` counted
from COMMITTED FileState truth (D-08, NOT the SAQ ledger); ``slots = cloud_max_in_flight - window``;
if ``slots <= 0`` stage nothing. Otherwise SELECT up to ``slots`` AWAITING_CLOUD files
``ORDER BY created_at ASC`` (FIFO) ``FOR UPDATE SKIP LOCKED``, flip each to ``PUSHING`` and enqueue
``push_file`` on the FILESERVER agent's per-agent queue. The COUNT + SELECT + ``state=PUSHING`` all
happen in ONE transaction so a 144-file backlog can never stage more than ``slots`` at a time
(T-50-scratch-dos) -- the committed PUSHING transition makes the next tick's window count current.

TWO gates, both a clean no-op (NOT a raise -- T-50-cron-raise) when the agent is absent:
  1. COMPUTE agent (the analysis consumer): no compute online -> ``{"staged": 0, "skipped": 0}``,
     files stay AWAITING_CLOUD.
  2. FILESERVER agent (the push initiator -- it owns the media mount and runs rsync): absent during
     a rolling restart -> ``{"staged": 0, "skipped": len(candidates)}``, files stay AWAITING_CLOUD
     and re-stage on a later tick.

A double-tick collapses via the deterministic ``push_file:<id>`` key (SAQ dedups the repeat enqueue
to ``None`` -> counted as skipped); the file still flips to PUSHING (the already-live push job will
land it), so the window stays honest.

CONTROL-ONLY: needs both PostgreSQL (``ctx["async_session"]``) and the per-agent enqueuer
(``ctx["task_router"]``), exactly like ``recover_orphaned_work``. Register ONLY in
``phaze.tasks.controller`` -- never the agent worker (``tests/test_task_split.py`` enforces the
agent role stays Postgres-free). FastAPI-free: imports neither ``fastapi`` nor ``phaze.routers``,
mirroring the ``recover_orphaned_work`` import discipline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import text
import structlog

from phaze.config import get_settings
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import get_cloud_staging_candidates, get_cloud_window_count


if TYPE_CHECKING:
    import uuid

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)

# WR-04: a fixed transaction-scoped advisory-lock key that serializes overlapping staging ticks so
# the load-bearing ≤N window cannot be overshot. SAQ does not guarantee non-overlapping cron runs,
# and the window COUNT reads COMMITTED truth -- so two ticks could each read window=0, SKIP LOCKED
# past each other's uncommitted PUSHING flips, and stage up to 2x cloud_max_in_flight. Holding this
# lock across the count+claim makes the second tick block until the first commits, after which it
# sees the committed window. Arbitrary stable constant (phase 50, plan 04); never collides because
# no other code path takes an advisory lock.
_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504


def push_file_job_key(file_id: uuid.UUID) -> str:
    """Return the deterministic SAQ job key ``push_file:<file_id>`` for a staged push.

    Mirrors ``analysis_enqueue.process_file_job_key``: a double cron tick (or a retried tick) of an
    already-staged file dedups to a no-op via SAQ's per-queue incomplete-set (T-50-double-enqueue).
    ``file_id`` is a server-generated UUID -- no untrusted free-text enters the key.
    """
    return f"push_file:{file_id}"


async def stage_cloud_window(ctx: dict[str, Any]) -> dict[str, int]:
    """Top the ≤N cloud window up to ``cloud_max_in_flight`` by staging ``push_file`` for held files.

    See the module docstring for the full window math + gate semantics. Returns
    ``{"staged": N, "skipped": M}`` where ``staged`` counts push_file jobs actually enqueued and
    ``skipped`` counts deterministic-key dedup no-ops (or, when the fileserver gate holds, the held
    candidate count). Every early-return path (no compute, window full, no candidates, no fileserver)
    is a clean no-op that leaves the held files in AWAITING_CLOUD for a later tick.
    """
    # cloud_max_in_flight lives on ControlSettings; this cron is registered ONLY on the control
    # worker (PHAZE_ROLE=control), so get_settings() returns ControlSettings here (mirrors the
    # controller.startup llm_model/llm_max_rpm access pattern).
    cfg = cast("ControlSettings", get_settings())
    # Phase 67 (D-14, REG-04): registry on/off gate. An all-local registry (cloud_enabled False) ->
    # clean no-op BEFORE the advisory lock + window logic, so the cron introduces NO new cloud push
    # work. Byte-identical to the former ``== "local"`` selector for the all-local deploy. NEVER raise
    # (T-50-cron-raise discipline, matching the GATE 1/2 no-op contract below).
    if not cfg.cloud_enabled:
        return {"staged": 0, "skipped": 0}
    # Resolve the single active dispatch backend through the Backend protocol (Phase 68, BACK-01): the
    # if/elif cloud-kind fork is gone. The cloud_enabled gate above short-circuited the all-local case,
    # so resolve_backends() yields exactly one non-local backend (its ≤1-non-local boot guard, D-07).
    # Imported at call time to keep the module-level import graph acyclic (backends.py re-homes this
    # module's deterministic push-job key + compute enqueue leg, so it imports this module).
    from phaze.services.backends import LocalBackend, resolve_backends  # noqa: PLC0415 -- deferred to break the backends<->drain import cycle

    backend = next(b for b in resolve_backends(cfg) if not isinstance(b, LocalBackend))
    # backend.cap is the ≤1-non-local reduction of the former cloud_max_in_flight (D-02a: nothing
    # consults per-backend in_flight_count for cap consumption yet -- that flip is Phase 69 / SCHED-02).
    max_in_flight = backend.cap

    async with ctx["async_session"]() as session:
        # WR-04: serialize overlapping cron ticks. A transaction-scoped advisory lock makes the
        # window count + candidate claim below atomic with respect to a concurrent tick: the second
        # tick blocks here until the first commits, then its get_cloud_window_count sees the
        # committed PUSHING flips and cannot overshoot cloud_max_in_flight. Auto-released at txn end.
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})

        # GATE 1: the backend must be able to accept a dispatch right now. The former compute-only
        # per-kind agent-gate fork is now backend.is_available (D-01a asymmetry
        # preserved inside the protocol): ComputeAgentBackend.is_available requires a live compute agent
        # (else every a1 file would wedge in AWAITING_CLOUD forever, Landmine L2); KueueBackend.is_available
        # probes the cluster with NO compute dependency (ephemeral Kueue pods); LocalBackend is always up.
        # A False degrades to a clean no-op hold (is_available never raises -- cron no-op discipline).
        # GATE 2 (fileserver) below stays for BOTH kinds (the fileserver owns the media mount + upload).
        if not await backend.is_available(session):
            logger.info("stage_cloud_window no-op: backend not available", backend_id=backend.id)
            return {"staged": 0, "skipped": 0}

        # Window counted from COMMITTED FileState truth (D-08); compute the free slots.
        window = await get_cloud_window_count(session)
        slots = max_in_flight - window
        if slots <= 0:
            return {"staged": 0, "skipped": 0}

        # FIFO oldest-first candidates, bounded to the free slots, row-locked (one transaction).
        candidates = await get_cloud_staging_candidates(session, slots)
        if not candidates:
            return {"staged": 0, "skipped": 0}

        # GATE 2: a fileserver agent (the push initiator) must be online. Absent during a rolling
        # restart -> clean hold no-op; the locked candidates stay AWAITING_CLOUD (no state change).
        try:
            fileserver_agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("stage_cloud_window hold: no fileserver agent online", candidates=len(candidates))
            return {"staged": 0, "skipped": len(candidates)}

        # Phase 68 (BACK-01): the per-file if/elif cloud-kind fork is now a single backend.dispatch()
        # call. dispatch owns the FileState -> PUSHING flip AND the cloud_job write in THIS session
        # (D-03), before its enqueue, so the drain's SINGLE post-loop commit stays the atomic boundary
        # -- dispatch NEVER commits (a mid-loop commit would release the advisory lock + row locks and
        # re-open the over-stage class, Landmine L1). dispatch returns True for a genuine stage and
        # False for a deterministic-key dedup no-op, preserving the Phase-50 staged/skipped tally.
        task_router = ctx["task_router"]
        tally = {"staged": 0, "skipped": 0}
        for index, file in enumerate(candidates):
            # WR-02: ComputeAgentBackend.dispatch re-resolves the fileserver agent per file (the push
            # initiator). Under READ COMMITTED a fileserver revoked by a concurrent session AFTER GATE-2
            # above but BEFORE this iteration makes select_active_agent raise NoActiveAgentError straight
            # out of dispatch. Catch it here so the cron degrades to a CLEAN HOLD of the not-yet-dispatched
            # candidates (T-50-cron-raise: the cron NEVER raises). dispatch resolves the fileserver BEFORE
            # any mutation, so the raising file is untouched; break and count the remaining candidates
            # (this one included) as skipped -- they stay AWAITING_CLOUD and re-stage on a later tick.
            try:
                dispatched = await backend.dispatch(file, session, task_router)
            except NoActiveAgentError:
                remaining = len(candidates) - index
                logger.info("stage_cloud_window hold: fileserver agent vanished mid-tick", held=remaining)
                tally["skipped"] += remaining
                break
            if dispatched:
                tally["staged"] += 1
            else:
                tally["skipped"] += 1
        await session.commit()

    logger.info("stage_cloud_window complete", agent_id=fileserver_agent.id, staged=tally["staged"], skipped=tally["skipped"])
    return tally
