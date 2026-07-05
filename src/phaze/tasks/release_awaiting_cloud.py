"""Control-side tiered multi-backend drain: route AWAITING_CLOUD files across N backends (Phase 69).

THE SINGLE "STAY ONE AHEAD" DRIVER (CLOUDPIPE-01/-05, SCHED-01/-02). Phase 49's per-file router HOLDS
every cloud-routed long file in ``FileState.AWAITING_CLOUD`` (it enqueues NOTHING directly -- that path
was removed in Phase 50 so the window cannot be bypassed). This cron is the ONLY thing that introduces
new dispatch work: every ~5 min it tops each backend's in-flight window up to its per-backend ``cap`` by
dispatching the oldest held files, rank-first. Registered as a SINGLE narrow
``CronJob(stage_cloud_window, "*/5 * * * *")`` on the controller.

Phase 69 tiered scheduler (SCHED-01/-02, D-05): the Phase-50 single-backend window is generalized.
ONCE per tick, under the single advisory lock, the drain snapshots EVERY resolved backend's
``is_available()`` and ``remaining = cap - in_flight_count()`` (the per-backend ``cloud_job``-derived
count that D-05-retired the global FileState ``{PUSHING, PUSHED}`` window). It then SELECTs up to
``sum(remaining over available backends)`` AWAITING_CLOUD files ``ORDER BY created_at ASC`` (FIFO)
``FOR UPDATE SKIP LOCKED`` and routes each candidate to the pure ``select_backend`` policy's rank-first
choice, calling ``backend.dispatch()`` and decrementing that backend's local ``remaining``. The
snapshot + SELECT + all dispatches happen in ONE transaction with ONE post-loop commit, so two
overlapping ticks serialize on the lock and no backend's ``cap`` is ever overshot (SCHED-02); a full
top-rank backend spills the candidate to the next rank within the same tick (SCHED-01).

TWO no-op gates, both a clean hold (NOT a raise -- T-50-cron-raise) when unavailable:
  1. PER-BACKEND availability (the snapshot): a backend whose ``is_available()`` is False (compute:
     no compute agent; kueue: cluster unreachable; local: always up) contributes 0 free slots. When
     ALL backends are down/full the tick is a clean no-op ``{"staged": 0, "skipped": 0}``.
  2. FILESERVER agent (the push initiator -- it owns the media mount and runs rsync/upload): absent
     during a rolling restart -> ``{"staged": 0, "skipped": len(candidates)}``, files stay
     AWAITING_CLOUD and re-stage on a later tick.

A double-tick collapses via the deterministic ``push_file:<id>`` key (SAQ dedups the repeat enqueue
to ``None`` -> counted as skipped); the file still flips to PUSHING (the already-live job lands it),
so the per-backend in-flight count stays honest. ``select_backend`` returning ``None`` is a clean hold
(the file stays AWAITING_CLOUD this tick, no writer touches the parked row -- its ``updated_at`` is the
staleness clock the spill-to-local gate reads).

CONTROL-ONLY: needs both PostgreSQL (``ctx["async_session"]``) and the per-agent enqueuer
(``ctx["task_router"]``), exactly like ``recover_orphaned_work``. Register ONLY in
``phaze.tasks.controller`` -- never the agent worker (``tests/test_task_split.py`` enforces the
agent role stays Postgres-free). FastAPI-free: imports neither ``fastapi`` nor ``phaze.routers``,
mirroring the ``recover_orphaned_work`` import discipline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import get_cloud_staging_candidates
from phaze.services.route_control import get_route_control


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.services.backend_selection import BackendSlot


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


async def _cloud_attempts_for(session: AsyncSession, file_id: uuid.UUID) -> int:
    """Return the file's ``cloud_job.attempts`` (0 when it has never been dispatched -> no cloud_job row).

    The pure ``select_backend`` policy reads this per candidate to enforce D-04 attempt-exclusion (a
    file that has spent its cloud budget routes to local only). Attempts live on the ``cloud_job``
    sidecar, not on ``FileRecord`` (Plan 01 signature note), so the drain looks them up here.
    """
    return int((await session.execute(select(CloudJob.attempts).where(CloudJob.file_id == file_id))).scalar() or 0)


async def stage_cloud_window(ctx: dict[str, Any]) -> dict[str, int]:
    """Top each backend's in-flight window up to its ``cap`` by dispatching held files, rank-first.

    See the module docstring for the full tiered-scheduler semantics. Returns
    ``{"staged": N, "skipped": M}`` where ``staged`` counts dispatches that enqueued new work and
    ``skipped`` counts deterministic-key dedup no-ops, clean per-candidate holds (``select_backend``
    -> ``None``), and the held candidate count when the fileserver gate holds. Every early-return path
    (cloud disabled, all backends full/down, no candidates, no fileserver) is a clean no-op that leaves
    the held files in AWAITING_CLOUD for a later tick. NEVER raises (T-50-cron-raise discipline).
    """
    # ControlSettings-scoped: this cron is registered ONLY on the control worker (PHAZE_ROLE=control),
    # so get_settings() returns ControlSettings here (mirrors controller.startup's config access).
    cfg = cast("ControlSettings", get_settings())
    # Phase 67 (D-14, REG-04): registry on/off gate. An all-local registry (cloud_enabled False) ->
    # clean no-op BEFORE the advisory lock + snapshot, so the cron introduces NO new dispatch work.
    if not cfg.cloud_enabled:
        return {"staged": 0, "skipped": 0}
    # Deferred imports (Pitfall: keep the module graph acyclic): backends.py re-homes this module's
    # push-job key + compute enqueue leg (it imports this module), and backend_selection imports
    # backends -- importing either at module top would close the backends<->drain import cycle.
    from phaze.services.backend_selection import select_backend  # noqa: PLC0415 -- deferred to break the import cycle
    from phaze.services.backends import resolve_backends  # noqa: PLC0415 -- deferred to break the import cycle

    # Phase 69 (SCHED-01): resolve ALL backends (resolve_backends no longer raises on >1 non-local).
    backends = resolve_backends(cfg)

    async with ctx["async_session"]() as session:
        # Phase 71 (BEUI-02, D-08): the force-local override. When engaged, this drain becomes a clean
        # no-op BEFORE the advisory lock + snapshot -- exactly like the cloud_enabled=False early return
        # above -- so it introduces NO new dispatch work and already-held AWAITING_CLOUD files stay held
        # (runbook A4). get_route_control degrades to False (cloud-enabled) on any DB error, so a hiccup
        # never crashes the cron (T-71-03).
        if await get_route_control(session):
            return {"staged": 0, "skipped": 0}
        # WR-04 / SCHED-02: one transaction-scoped advisory lock serializes overlapping ticks so no
        # backend's cap is overshot. The second tick blocks here until the first commits, then its
        # once-per-tick in_flight_count snapshot sees the committed dispatches. Auto-released at txn end.
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})

        # SCHED-02 / Pitfall 1: snapshot EVERY backend's availability + free capacity ONCE per tick.
        # is_available (the Kueue cluster probe / compute agent gate / local always-up) and
        # in_flight_count (the D-05 per-backend cloud_job count replacing the retired global window) are
        # each probed exactly M times HERE -- NEVER re-probed inside the candidate loop below.
        snapshot: dict[str, BackendSlot] = {}
        for backend in backends:
            # MKUE-03 / D-07 (research Pitfall 8): per-backend failure isolation for the once-per-tick
            # snapshot. is_available / in_flight_count are SUPPOSED to swallow their own failures (Phase
            # 68's "is_available never raises" discipline), but a raise or timeout that escapes ONE flaky
            # cluster's probe must NOT abort the whole drain tick -- it would starve every healthy backend
            # (and local) of work. Treat a raising/timing-out backend as UNAVAILABLE (0 free slots) for
            # this tick and log (backend_id only -- never a KubeConfig / SecretStr / exception payload
            # carrying creds, T-70-03-02), then continue so the surrounding limit-gate simply sees this
            # backend contribute nothing while every other backend proceeds normally.
            try:
                available = await backend.is_available(session)
                remaining = max(0, backend.cap - await backend.in_flight_count(session))
            except Exception:
                logger.warning("stage_cloud_window: backend snapshot probe failed -> treating as unavailable (0 slots)", backend_id=backend.id)
                snapshot[backend.id] = {"backend": backend, "available": False, "remaining": 0, "cap": backend.cap}
                continue
            snapshot[backend.id] = {
                "backend": backend,
                "available": available,
                "remaining": remaining,
                "cap": backend.cap,
            }

        # Candidate limit = total free slots across all AVAILABLE backends (non-local free capacity +
        # local headroom). GATE 1 for the whole registry: when every backend is full or unavailable the
        # limit is 0 -> clean no-op (no candidate is fetched, no state changes).
        limit = sum(slot["remaining"] for slot in snapshot.values() if slot["available"])
        if limit <= 0:
            return {"staged": 0, "skipped": 0}

        # FIFO oldest-first candidates, bounded to the total free slots, row-locked (one transaction).
        candidates = await get_cloud_staging_candidates(session, limit)
        if not candidates:
            return {"staged": 0, "skipped": 0}

        # GATE 2: a fileserver agent (the push initiator -- owns the media mount + rsync/upload) must be
        # online for ANY backend to dispatch. Absent during a rolling restart -> clean hold no-op; the
        # locked candidates stay AWAITING_CLOUD (no state change). Never a raise (T-50-cron-raise).
        try:
            fileserver_agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("stage_cloud_window hold: no fileserver agent online", candidates=len(candidates))
            return {"staged": 0, "skipped": len(candidates)}

        # SCHED-01: route each FIFO candidate to select_backend's rank-first choice across N backends.
        # dispatch owns the FileState -> PUSHING flip AND the cloud_job write in THIS session (D-03),
        # before its enqueue, so the SINGLE post-loop commit stays the atomic boundary -- dispatch NEVER
        # commits (a mid-loop commit would release the advisory lock + row locks and re-open the
        # over-stage class, Landmine L1). A truthy return is a genuine stage; False is a deterministic-key
        # dedup no-op; either way the cloud_job slot was claimed, so the local remaining is decremented.
        task_router = ctx["task_router"]
        tally = {"staged": 0, "skipped": 0}
        # CR-02 safety net (T-50-cron-raise): the candidate loop + the single post-loop commit run under
        # ONE outer guard so an UNEXPECTED raise -- e.g. a Postgres serialization/deadlock surfaced from a
        # session.execute mid-loop (which aborts the txn, so every subsequent statement INCLUDING the final
        # commit raises), or a raise from _cloud_attempts_for outside the per-candidate try below -- can
        # NEVER propagate out of this cron. On any such error we roll back the WHOLE tick (discarding every
        # partial/uncommitted write so no phantom dispatch is ever committed) and report a clean hold; the
        # held candidates stay AWAITING_CLOUD and re-stage next tick. This is the ONLY rollback: we never
        # roll back mid-loop (that would end the txn and release the pg_advisory_xact_lock, re-opening the
        # over-stage class, Landmine L1). The advisory-lock scope + single post-loop commit are unchanged.
        try:
            for index, file in enumerate(candidates):
                cloud_attempts = await _cloud_attempts_for(session, file.id)
                # models/base.py: created_at/updated_at carry no timezone=True, so create_all yields naive
                # datetimes while a TIMESTAMPTZ migration column hands asyncpg tz-aware ones. Match the
                # candidate's awareness (assume-UTC, the scan_reaper / pipeline_scans convention) so the pure
                # select_backend staleness subtraction (now - file.updated_at) never raises -- WITHOUT
                # mutating the parked AWAITING_CLOUD row (its updated_at is the staleness clock, RESEARCH A3).
                now = datetime.now(UTC)
                if file.updated_at.tzinfo is None:
                    now = now.replace(tzinfo=None)
                target = select_backend(file, cloud_attempts, snapshot, now, cfg)
                if target is None:
                    # Clean per-candidate hold: no eligible backend for this file this tick. No state change
                    # -- the file stays AWAITING_CLOUD (guards the updated_at staleness signal, RESEARCH A3).
                    tally["skipped"] += 1
                    continue
                # WR-02: dispatch re-resolves the fileserver agent per file. Under READ COMMITTED a fileserver
                # revoked by a concurrent session AFTER GATE-2 above but BEFORE this iteration raises
                # NoActiveAgentError straight out of dispatch. Catch it -> CLEAN HOLD of the remaining
                # not-yet-dispatched candidates (this one included), which stay AWAITING_CLOUD (cron NEVER
                # raises). Every dispatch gates its fileserver agent BEFORE any state mutation (CR-01), so the
                # raising file is untouched and the already-staged prior candidates are genuine -> break (NOT
                # rollback), letting the post-loop commit persist that good prior work.
                try:
                    dispatched = await target.dispatch(file, session, task_router)
                except NoActiveAgentError:
                    remaining = len(candidates) - index
                    logger.info("stage_cloud_window hold: fileserver agent vanished mid-tick", held=remaining)
                    tally["skipped"] += remaining
                    break
                except Exception:
                    # MKUE-03 / D-07 (research Pitfall 8): a GENERIC kube/S3 raise from ONE backend's dispatch
                    # (a cluster/bucket error, NOT the fileserver-vanish NoActiveAgentError above) is a clean
                    # hold of THIS candidate ONLY -- distinct from the fileserver-vanish break, which affects
                    # every remaining dispatch. Each dispatch gates its fileserver agent + runs its fallible
                    # S3 setup BEFORE the FileState flip (CR-01), so the common pre-upsert raise touches
                    # nothing and the file stays AWAITING_CLOUD. We do NOT roll back here (a mid-loop rollback
                    # would drop the advisory lock, Landmine L1); if the raise instead POISONED the txn (a PG
                    # statement error), the outer safety net rolls the whole tick back so nothing partial is
                    # committed. Count it skipped, log (backend_id only, T-70-03-02), do NOT decrement the slot
                    # (no work claimed), and continue so a single flaky cluster cannot starve the other
                    # backends. The tick NEVER aborts and NEVER raises.
                    logger.warning("stage_cloud_window: backend dispatch failed -> holding this candidate", backend_id=target.id)
                    tally["skipped"] += 1
                    continue
                # The slot is claimed (cloud_job upserted) on both a genuine stage and a dedup no-op, so
                # decrement the local remaining unconditionally -- this is what makes a full top-rank backend
                # spill the NEXT candidate to the next rank within the same tick (SCHED-01), cap-safe (SCHED-02).
                snapshot[target.id]["remaining"] -= 1
                if dispatched:
                    tally["staged"] += 1
                else:
                    tally["skipped"] += 1
            await session.commit()
        except Exception:
            # A poisoned transaction (or any unexpected raise from the pre-dispatch loop body / the commit)
            # must degrade to a clean hold, never a cron raise. Roll back the whole tick -- this discards any
            # uncommitted partial write, so a dispatch that raised AFTER a DB mutation can never leave a
            # committed limbo/phantom row -- and report every candidate held; they stay AWAITING_CLOUD.
            logger.warning("stage_cloud_window: tick aborted by an unexpected error -> rolling back, holding all", exc_info=True)
            await session.rollback()
            return {"staged": 0, "skipped": len(candidates)}

    logger.info("stage_cloud_window complete", agent_id=fileserver_agent.id, staged=tally["staged"], skipped=tally["skipped"])
    return tally
