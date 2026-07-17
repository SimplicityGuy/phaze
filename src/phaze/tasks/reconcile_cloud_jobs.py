"""Narrow ``*/5`` in-flight K8s reconcile cron (Phase 54, Plan 06 -- KSUBMIT-02..06, D-01..D-08).

THE SAFETY NET THAT OWNS THE KUEUE JOB LIFECYCLE. ``submit_cloud_job`` (Plan 05) does ONE fast kube
POST and returns; the one-shot pod runs the analysis and PUTs its result back through the existing
``/api/internal/agent/analysis/{file_id}`` callback -- which is the SOLE authoritative result writer
(KSUBMIT-03). This cron NEVER writes an analysis result. It is a cron-only POLL (D-01): every tick it
re-reads the in-flight Jobs/Workloads; there is NO live kube watch stream.

Iteration source (D-02): ``SELECT cloud_job WHERE status IN (SUBMITTED, RUNNING)`` -- the durable
``cloud_job`` sidecar is the in-flight registry, NOT a kube watch and NOT ``recover_orphaned_work``.
For each row it reads the Job (succeeded/failed -- the most direct terminal signal) and, when the Job
is not yet terminal, the paired Kueue Workload (admission state: Pending vs Inadmissible vs Evicted vs
Admitted) and maps the ``(type, status, reason)`` condition tuples to an outcome (RESEARCH
§Status->Outcome Mapping).

The load-bearing correctness property is the delete-after-record ordering (D-04): on a terminal
outcome phaze records the result in the DB and COMMITS *before* it deletes the Job, so the status read
can never lose to GC -- ``ttlSecondsAfterFinished`` (900s) is only the never-reconciled backstop. On a
no-callback terminal (Failed/Evicted) it also deletes the staged S3 object (D-05); the success path
does NOT (the callback already deleted it inline). A no-callback terminal under the cap re-drives a
fresh ``submit_cloud_job`` (D-08); at the cap the cloud_job sidecar is re-stamped ``status='awaiting'`` via
the single spill-mode writer (``hold_awaiting_cloud``, D-04/D-12) with NO ``FileRecord.state`` write, so the
next drain tick routes it to the local safety net (``attempts >= cap``) rather than hard-failing.
Inadmissible (operator misconfig) holds indefinitely + alerts and NEVER consumes
the cap (D-06/D-07); healthy Pending is silent.

CONTROL-ONLY: needs PostgreSQL (``ctx["async_session"]``) + the controller queue (``ctx["queue"]``) for
the re-drive enqueue, and the kube surface via ``kube_staging`` -- exactly like ``stage_cloud_window`` /
``recover_orphaned_work``. Register ONLY in ``phaze.tasks.controller`` (``tests/shared/core/test_task_split.py``
enforces the agent worker stays free of it). FastAPI-free: imports neither ``fastapi`` nor
``phaze.routers``. DO NOT re-add a general auto-advance / ``recover_orphaned_work`` cron here -- this is
narrow, in-flight K8s reconcile ONLY (mirror the ``controller.py`` cron-scope guard comments).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, cast

import kr8s
from sqlalchemy import select
import structlog

from phaze.config import get_settings
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.services import kube_staging, s3_staging
from phaze.tasks.submit_cloud_job import submit_cloud_job_key


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import KubeConfig


logger = structlog.get_logger(__name__)


# The Kueue Workload condition vocabulary the loop matches (RESEARCH §Status->Outcome Mapping,
# verified against Context7 /kubernetes-sigs/kueue). Matching the exact (type, status, reason) tuples
# is what keeps healthy Pending from being mistaken for a fault (Pitfall 3).
_TYPE_QUOTA_RESERVED = "QuotaReserved"
_TYPE_ADMITTED = "Admitted"
_TYPE_EVICTED = "Evicted"
_REASON_PENDING = "Pending"
_REASON_INADMISSIBLE = "Inadmissible"


def _job_counter(job: Any, key: str) -> int:
    """Read an integer ``status`` counter (``succeeded``/``failed``) off a Job, defaulting to 0.

    kr8s exposes ``.status`` as a dict; with ``backoffLimit: 0`` a non-zero ``succeeded``/``failed`` is
    the most direct terminal signal (the Job is the source of truth for succeeded-vs-failed).
    """
    status = getattr(job, "status", None) or {}
    try:
        return int(status.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _job_has_true_condition(job: Any, cond_type: str) -> bool:
    """Return whether the Job carries a ``(cond_type, status=True)`` entry in ``status.conditions``."""
    status = getattr(job, "status", None) or {}
    return any(cond.get("type") == cond_type and cond.get("status") == "True" for cond in status.get("conditions", []) or [])


def _workload_condition(workload: Any, cond_type: str) -> dict[str, Any] | None:
    """Return the first ``status.conditions`` entry of ``cond_type`` on a Kueue Workload, or None."""
    status = getattr(workload, "status", None) or {}
    for cond in status.get("conditions", []) or []:
        if cond.get("type") == cond_type:
            return cast("dict[str, Any]", cond)
    return None


async def _job_gone(name: str, kube: KubeConfig) -> bool:
    """Return whether the Job ``name`` is gone (deleted) on ``kube``'s cluster -- ``get_job`` returns None or 404s.

    The re-drive race guard (D-08): after ``delete_job`` we confirm the prior Job is GONE before
    enqueuing the fresh ``submit_cloud_job``. If it is still terminating, the deterministic-name
    409->refresh inside ``submit_job`` would re-acquire the still-present Failed Job and the next tick
    would re-see Failed and burn an extra attempt. A real ``get_job`` raises ``NotFoundError`` on a 404
    (the desired end state); the fake-kube seam returns None.
    """
    try:
        job = await kube_staging.get_job(name, kube)
    except kr8s.NotFoundError:
        return True
    return job is None


async def _analysis_completed(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Return whether the file's analysis result already landed (``analysis_completed_at IS NOT NULL``).

    phaze-2o8p: the ``/api/internal/agent/analysis/{file_id}`` callback (KSUBMIT-03) stamps
    ``analysis_completed_at`` and deletes the staged S3 object, but NEVER advances ``cloud_job.status``.
    A callback-completed file therefore sits SUBMITTED/RUNNING until reconcile next reads its Job. If
    the reconcile lag exceeds ``ttlSecondsAfterFinished`` (900s) the succeeded Job is GC'd, so the
    vanished-Job path would misclassify a DONE file as a no-callback terminal and re-drive it against a
    staged object the callback already deleted. This lets that path recognise the success instead.
    """
    completed_at = (
        await session.execute(select(AnalysisResult.analysis_completed_at).where(AnalysisResult.file_id == file_id))
    ).scalar_one_or_none()
    return completed_at is not None


async def _enqueue_resubmit(ctx: dict[str, Any], file_id: uuid.UUID) -> None:
    """Enqueue a fresh ``submit_cloud_job`` on the controller queue with the deterministic dedup key.

    The re-drive (D-08) is a fresh submit on the controller queue (where the kube creds live);
    ``submit_cloud_job_key`` collapses a still-live submit to a no-op (mirrors the staging-cron dedup).
    Writes NO scheduling-ledger row (KSUBMIT-06) -- the re-drive routes the SAME ``submit_cloud_job``
    that the cloud_job sidecar tracks, never a ``process_file`` ledger seed.
    """
    queue = ctx["queue"]
    await queue.connect()
    await queue.enqueue("submit_cloud_job", key=submit_cloud_job_key(file_id), file_id=str(file_id))


async def _record_success(session: AsyncSession, cloud_job: CloudJob, name: str, tally: dict[str, int], kube: KubeConfig) -> None:
    """Succeeded Job: record SUCCEEDED + COMMIT, THEN delete the Job on ``kube``'s cluster (D-04). No S3 delete, no result.

    The analysis result already landed via the ``/api/internal/agent/*`` callback (KSUBMIT-03), which
    also deleted the staged S3 object inline (D-05) -- so the success path makes ZERO S3 calls and
    NEVER writes an analysis result. Recording + committing before the delete means the status read can
    never lose to GC.
    """
    cloud_job.status = CloudJobStatus.SUCCEEDED.value
    cloud_job.inadmissible = False  # CR-01: a transiently-Inadmissible row that then succeeds must clear the alert flag.
    cloud_job.cloud_phase = CloudPhase.FINISHED.value  # D-04: admission progression terminus (orthogonal to the fault flag).
    await session.commit()
    await kube_staging.delete_job(name, kube)
    tally["succeeded"] += 1


async def _handle_no_callback_terminal(
    ctx: dict[str, Any],
    session: AsyncSession,
    cloud_job: CloudJob,
    name: str,
    cap: int,
    tally: dict[str, int],
    kube: KubeConfig,
) -> None:
    """Failed/Evicted (no-callback terminal): bounded re-drive under cap, spill the sidecar to 'awaiting' at cap (D-08/SCHED-03).

    At cap (``attempts + 1 > cloud_submit_max_attempts``) the ordering is the load-bearing terminal
    sequence (MKUE-04 clean-before-flip, D-01/D-03/D-04): capture the OLD (backend_id, staging_bucket)
    identity, ``delete_staged_object`` the old object UNDER the still-held per-row advisory lock (before
    the spill commit) -> re-stamp the cloud_job sidecar ``status='awaiting'`` via the single spill-mode
    writer (``hold_awaiting_cloud``, D-04/D-12 -- reconcile writes NO ``FileRecord.state``) + clear
    ``staging_bucket`` + COMMIT (which releases the lock) -> ``delete_job`` (post-commit). Deleting the old
    object before the commit that makes the file a drain candidate closes Pitfall 9: a concurrent drain
    tick cannot re-dispatch + re-stage a new object under the same ``file_id`` key until this txn commits,
    so the trailing delete can never destroy the new owner's object. The file is NOT hard-failed on cloud
    flakiness (SCHED-03): because ``cloud_job.attempts`` already equals ``cap``, the next drain tick's
    ``select_backend`` excludes every cloud backend (``attempts >= cap``) and routes the file to the local
    safety net -- ANALYSIS_FAILED then comes only from a local failure (D-04), never from this branch. The
    spilled kueue file stays at its prior ``PUSHED`` state (reconcile no longer touches ``FileRecord``),
    which satisfies the loosened pushed/pushing shadow invariants -- fixing the HARD ``state=AWAITING_CLOUD``
    + ``cloud_job.status=FAILED`` shadow violation that is live on ``main`` today.

    Under cap it is a re-drive: delete the prior Job and CONFIRM it is gone (the race guard) BEFORE
    incrementing ``attempts`` + committing and enqueuing the fresh ``submit_cloud_job``. If the prior
    Job is still terminating the re-drive is deferred to a later tick with NO state change -- so no
    extra attempt is burned and the deterministic-name 409->refresh cannot latch onto the dying Job.
    The staged S3 object is PRESERVED on the re-drive path (the re-submitted Job still needs it); it is
    deleted only on the genuinely-terminal at-cap path.
    """
    file_id = cloud_job.file_id
    next_attempt = cloud_job.attempts + 1

    if next_attempt > cap:
        # SCHED-03/D-04: at the cloud cap DO NOT hard-fail. Re-stamp the cloud_job sidecar to 'awaiting'
        # ('awaiting' is NOT in IN_FLIGHT, so the row drops out of ``in_flight_count`` -- the
        # reconcile-only-decrements invariant) and write NO FileRecord.state (D-04, the whole point of the
        # cutover). ``cloud_job.attempts`` already equals ``cap`` here (the last under-cap re-drive set it),
        # so the next drain tick's ``select_backend`` sees ``attempts >= cap`` and routes the file to local
        # (the guaranteed safety net) -- do NOT increment attempts again here (avoids a double-count). Local
        # failure, not cloud flakiness, is the only terminal into ANALYSIS_FAILED (D-04). The re-stamped
        # ``updated_at`` on the spill gives a fresh staleness clock (desirable).
        #
        # MKUE-04 clean-before-flip (D-01/D-03, Pitfall 9 -- the crux): the OLD (backend_id, staging_bucket)
        # staged object MUST be deleted WHILE the per-row ``pg_advisory_xact_lock(5_000_504)`` is still held
        # (acquired at the TOP of this ``KueueBackend.reconcile`` per-row unit, backends.py) -- i.e. BEFORE
        # the ``session.commit()`` that persists the 'awaiting' re-stamp (making the file a drain candidate)
        # and thus RELEASES the lock. The re-dispatch reuses the SAME ``file_id``-scoped S3 key; if D-06
        # lands the re-stage on the same bucket, a delete that ran AFTER the lock released would race the
        # new stage and destroy the object the new pod needs. Deleting before the flip guarantees the old
        # object is gone before any re-stage can occur (the drain holds the same lock across its whole
        # candidate claim, so it physically cannot pick up the file until this txn commits).
        #
        # Capture the OLD identity into locals BEFORE any mutation, resolve the RECORDED staging bucket
        # (never re-derive -- Pitfall 4/T-70-04-04), and delete it UNDER the lock. The delete is best-effort
        # (D-03): ``contextlib.suppress(Exception)`` so a slow/failed/absent S3 delete never blocks the spill
        # nor pins the lock beyond one network timeout (the per-bucket TTL is the backstop). A bucketless row
        # (no staged object) resolves to None and skips the delete cleanly.
        cfg = cast("ControlSettings", get_settings())
        old_bucket_id = cloud_job.staging_bucket  # captured pre-mutation -- the authoritative old identity.
        bucket = s3_staging.resolve_bucket_config(cfg, old_bucket_id)
        with contextlib.suppress(Exception):
            if bucket is not None:
                await s3_staging.delete_staged_object(file_id, bucket)  # MKUE-04: under the still-held lock, BEFORE the commit.
        # D-04/D-12: swap the retired FileRecord.state write + FAILED pre-mutation for the SINGLE go-forward
        # awaiting writer in spill mode (reconcile is its FOURTH caller, alongside agent_s3/agent_push). The
        # rowcount-guarded CAS OWNS the status write (UPDATE cloud_job ... WHERE status IN (SUBMITTED,RUNNING)),
        # so we do NOT pre-mutate cloud_job.status here -- an autoflush of a dirty status would make the CAS
        # miss its own row (RESEARCH Landmine 3). ``attempts=cap`` is the budget-spent MARKER (a set, NOT an
        # increment), so the next drain tick's ``select_backend`` sees ``attempts >= cap`` and routes the file
        # to local; ``clear_cloud_phase=True`` nulls cloud_phase (WR-01, off the "Running" tile). Unlike the
        # agent_s3/agent_push siblings (which KEEP the gated FileRecord dual-write, 83 D-00c), reconcile writes
        # NO FileRecord.state at all (D-04): the spilled kueue file stays at its prior PUSHED state, which
        # satisfies the loosened pushed/pushing shadow invariants -- fixing the HARD state=AWAITING_CLOUD +
        # cloud_job.status=FAILED shadow violation live on main today.
        from phaze.services.backends import hold_awaiting_cloud  # noqa: PLC0415 -- deferred to break the backends<->reconcile_cloud_jobs import cycle

        # The helper's spill-mode CAS dereferences file.id (it does NOT write file.state); load the FileRecord.
        # The FK files.id <- cloud_job.file_id guarantees the row exists, so None is unreachable in practice --
        # the guard is for mypy (scalar_one_or_none is Optional) and defense-in-depth (a None file skips the CAS
        # cleanly, matching the agent_s3/agent_push no-op).
        file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
        if file is not None:
            await hold_awaiting_cloud(
                session,
                file,
                attempts=cap,
                expect_status=(CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value),
                clear_cloud_phase=True,
            )
        cloud_job.inadmissible = False  # terminal row must not keep the operator alert lit (helper does not stamp it).
        cloud_job.staging_bucket = None  # clear so no pre-repurpose reader is misled about the (now-gone) object.
        await session.commit()  # releases the per-row lock -- the old object is ALREADY gone (clean-before-flip).
        await kube_staging.delete_job(name, kube)  # Job delete stays POST-commit (D-04 status-read-vs-GC; cleanup only).
        tally["failed"] += 1
        logger.warning(
            "reconcile_cloud_jobs: submit cap reached -> cloud_job re-stamped 'awaiting' + spill to local",
            file_id=str(file_id),
            attempt=next_attempt,
            cap=cap,
        )
        return

    # Under cap -> re-drive. Delete the prior Job, then confirm it is gone before re-submitting.
    await kube_staging.delete_job(name, kube)
    if not await _job_gone(name, kube):
        logger.info("reconcile_cloud_jobs: prior Job still terminating; deferring re-drive", file_id=str(file_id), kueue_workload=name)
        return
    cloud_job.attempts = next_attempt
    cloud_job.status = CloudJobStatus.SUBMITTED.value
    cloud_job.inadmissible = False  # CR-01: re-driving a failed Job clears any stale Inadmissible flag.
    await session.commit()
    await _enqueue_resubmit(ctx, file_id)
    tally["redriven"] += 1
    logger.info("reconcile_cloud_jobs: re-driving submit_cloud_job", file_id=str(file_id), attempt=next_attempt)


async def _reconcile_one(ctx: dict[str, Any], session: AsyncSession, cloud_job: CloudJob, cap: int, tally: dict[str, int], kube: KubeConfig) -> None:
    """Reconcile a single in-flight ``cloud_job`` row against its Job + Kueue Workload on ``kube``'s cluster.

    Phase 70 (MKUE-01/D-04): ``kube`` is THIS row's owning backend ``KubeConfig`` (threaded from
    ``KueueBackend.reconcile``), so every ``get_job`` / ``get_workload_for`` / ``delete_job`` targets the
    file's own cluster.
    """
    name = cloud_job.kueue_workload
    if not name:
        logger.warning("reconcile_cloud_jobs: cloud_job missing kueue_workload; skipping", cloud_job_id=str(cloud_job.id))
        await session.commit()  # WR-01: no mutation, but release the per-row advisory lock (Pitfall 2).
        return

    # WR-01: a vanished Job (real kube 404 -> NotFoundError; fake seam -> None) on an in-flight row is a
    # no-callback terminal, NOT a transient error. Route it to the bounded re-drive / at-cap spill-back
    # handler instead of letting NotFoundError bubble to the per-row guard, where it would be rolled back
    # and skipped every tick -- leaving the row stuck in-flight forever (e.g. a Failed Job GC'd by
    # ttlSecondsAfterFinished before reconcile read it, or an enqueue that raised after the attempt commit).
    try:
        job = await kube_staging.get_job(name, kube)
    except kr8s.NotFoundError:
        job = None
    if job is None:
        # phaze-2o8p: distinguish a callback-completed-then-TTL-GC'd Job from a genuine no-callback
        # terminal. If the analysis result already landed (analysis_completed_at IS NOT NULL), the
        # vanished Job is a SUCCESS whose Job was reaped by ttlSecondsAfterFinished before this lagging
        # tick read it -- finalize it (record SUCCEEDED + delete Job) instead of re-driving an
        # already-analyzed file against a staged object the success callback already deleted.
        if await _analysis_completed(session, cloud_job.file_id):
            await _record_success(session, cloud_job, name, tally, kube)
            return
        await _handle_no_callback_terminal(ctx, session, cloud_job, name, cap, tally, kube)
        return

    # 1. Job terminal signals first -- the Job is the source of truth for succeeded-vs-failed.
    if _job_counter(job, "succeeded") >= 1 or _job_has_true_condition(job, "Complete"):
        await _record_success(session, cloud_job, name, tally, kube)
        return
    if _job_counter(job, "failed") >= 1 or _job_has_true_condition(job, "Failed"):
        await _handle_no_callback_terminal(ctx, session, cloud_job, name, cap, tally, kube)
        return

    # 2. Not terminal -> read the paired Kueue Workload for admission state (D-02 by job-uid).
    uid = str(getattr(getattr(job, "metadata", None), "uid", "") or "")
    workload = await kube_staging.get_workload_for(uid, kube) if uid else None
    if workload is None:
        # Admission state unreadable this tick (label miss + owner-ref miss) -> stay in-flight, no-op.
        await session.commit()  # WR-01: no mutation, but release the per-row advisory lock (Pitfall 2).
        return

    # Evicted/deactivated -> no-callback terminal (re-drive under cap).
    evicted = _workload_condition(workload, _TYPE_EVICTED)
    if evicted is not None and evicted.get("status") == "True":
        await _handle_no_callback_terminal(ctx, session, cloud_job, name, cap, tally, kube)
        return

    quota_reserved = _workload_condition(workload, _TYPE_QUOTA_RESERVED)

    # Inadmissible (operator misconfig): loud + hold, NEVER consumes the cap (D-06/D-07).
    if quota_reserved is not None and quota_reserved.get("status") == "False" and quota_reserved.get("reason") == _REASON_INADMISSIBLE:
        if not cloud_job.inadmissible:
            cloud_job.inadmissible = True
        await session.commit()  # WR-01: commit unconditionally (no-op when already flagged) to release the lock.
        tally["inadmissible"] += 1
        logger.warning(
            "reconcile_cloud_jobs: Workload Inadmissible -- K8s Jobs not admitting; check LocalQueue config",
            cloud_job_id=str(cloud_job.id),
            file_id=str(cloud_job.file_id),
            kueue_workload=name,
        )
        return

    # Healthy Pending: silent hold, waits indefinitely -- no cap, no alert (D-07, Pitfall 3).
    if quota_reserved is not None and quota_reserved.get("status") == "False" and quota_reserved.get("reason") == _REASON_PENDING:
        if cloud_job.inadmissible:  # CR-01: the misconfig was fixed -- Workload is back to a healthy quota wait.
            cloud_job.inadmissible = False
        if cloud_job.cloud_phase != CloudPhase.QUEUED_BEHIND_QUOTA.value:  # D-04: behind quota, waiting for admission.
            cloud_job.cloud_phase = CloudPhase.QUEUED_BEHIND_QUOTA.value
        # WR-01: commit unconditionally (a clean no-op when neither field changed) to release the per-row lock.
        await session.commit()
        tally["pending"] += 1
        return

    # Admitted / QuotaReserved=True -> in-flight running; advance SUBMITTED -> RUNNING.
    admitted = _workload_condition(workload, _TYPE_ADMITTED)
    admitted_true = admitted is not None and admitted.get("status") == "True"
    quota_true = quota_reserved is not None and quota_reserved.get("status") == "True"
    if admitted_true or quota_true:
        # D-04 admission progression (ORTHOGONAL to the status advance): Admitted=True means the pod
        # is un-gated and running -> RUNNING; QuotaReserved-only (quota granted, not yet un-suspended)
        # is the intermediate ADMITTED phase. The cloud_job ``status`` axis still advances to RUNNING
        # in both cases (unchanged).
        next_phase = CloudPhase.RUNNING.value if admitted_true else CloudPhase.ADMITTED.value
        if cloud_job.status != CloudJobStatus.RUNNING.value or cloud_job.inadmissible or cloud_job.cloud_phase != next_phase:
            cloud_job.status = CloudJobStatus.RUNNING.value
            cloud_job.inadmissible = False  # CR-01: an admitted Workload is no longer Inadmissible -- clear the alert.
            cloud_job.cloud_phase = next_phase
        # WR-01: commit unconditionally (a clean no-op when already RUNNING in the target phase) to release the lock.
        await session.commit()
        tally["running"] += 1
        return

    # Unknown in-flight condition set -> leave the row untouched for a later tick.
    await session.commit()  # WR-01: no mutation, but release the per-row advisory lock (Pitfall 2).


async def reconcile_cloud_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Reconcile every backend's in-flight ``cloud_job`` rows per-backend; return an aggregate tally.

    The ``*/5`` cron body (D-01/D-03), Phase-69 SCHED-05 form: dispatch reconcile PER-BACKEND
    (``for b in resolve_backends(cfg): await b.reconcile(session, ctx)``) instead of a single global
    ``select(CloudJob WHERE status IN {SUBMITTED, RUNNING})`` query. Removing that global un-scoped query
    closes the double-owner vector: a compute ``cloud_job`` row is now touched ONLY by its ``/pushed``
    callback (Compute/Local ``reconcile`` are no-ops); the Kueue rows are owned by ``KueueBackend.reconcile``
    (backend_id-scoped, per-row advisory-locked). Each backend's tally is aggregated into the cron's
    return dict (same shape); the per-row guard + delete-after-record ordering + "never raise out of the
    cron" discipline live inside each backend's ``reconcile`` (KSUBMIT-03: still never writes a result).

    ``resolve_backends`` is imported FUNCTION-LOCALLY (deferred) because ``services.backends`` does a
    module-top ``from phaze.tasks.reconcile_cloud_jobs import _reconcile_one`` -- a module-top import
    here would be a ``backends -> reconcile_cloud_jobs -> backends`` collection-time ImportError.
    """
    from phaze.services.backends import resolve_backends  # noqa: PLC0415 -- deferred to break the backends<->reconcile_cloud_jobs import cycle

    cfg = cast("ControlSettings", get_settings())
    tally = {"reconciled": 0, "succeeded": 0, "failed": 0, "redriven": 0, "inadmissible": 0, "pending": 0, "running": 0}

    async with ctx["async_session"]() as session:
        for backend in resolve_backends(cfg):
            backend_tally = await backend.reconcile(session, ctx)
            # Kueue returns its per-backend tally; Local/Compute reconcile are no-ops (None). Aggregate.
            if backend_tally:
                for key, value in backend_tally.items():
                    tally[key] = tally.get(key, 0) + value

    logger.info("reconcile_cloud_jobs complete", **tally)
    return tally
