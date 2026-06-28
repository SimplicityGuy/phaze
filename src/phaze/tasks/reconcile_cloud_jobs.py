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
fresh ``submit_cloud_job`` (D-08); at the cap the FileRecord is marked ANALYSIS_FAILED with NO
cross-target fallback. Inadmissible (operator misconfig) holds indefinitely + alerts and NEVER consumes
the cap (D-06/D-07); healthy Pending is silent.

CONTROL-ONLY: needs PostgreSQL (``ctx["async_session"]``) + the controller queue (``ctx["queue"]``) for
the re-drive enqueue, and the kube surface via ``kube_staging`` -- exactly like ``stage_cloud_window`` /
``recover_orphaned_work``. Register ONLY in ``phaze.tasks.controller`` (``tests/test_task_split.py``
enforces the agent worker stays free of it). FastAPI-free: imports neither ``fastapi`` nor
``phaze.routers``. DO NOT re-add a general auto-advance / ``recover_orphaned_work`` cron here -- this is
narrow, in-flight K8s reconcile ONLY (mirror the ``controller.py`` cron-scope guard comments).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import kr8s
from sqlalchemy import select, update
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.services import kube_staging, s3_staging
from phaze.tasks.submit_cloud_job import submit_cloud_job_key


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


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


async def _job_gone(name: str) -> bool:
    """Return whether the Job ``name`` is gone (deleted) -- ``get_job`` returns None or 404s.

    The re-drive race guard (D-08): after ``delete_job`` we confirm the prior Job is GONE before
    enqueuing the fresh ``submit_cloud_job``. If it is still terminating, the deterministic-name
    409->refresh inside ``submit_job`` would re-acquire the still-present Failed Job and the next tick
    would re-see Failed and burn an extra attempt. A real ``get_job`` raises ``NotFoundError`` on a 404
    (the desired end state); the fake-kube seam returns None.
    """
    try:
        job = await kube_staging.get_job(name)
    except kr8s.NotFoundError:
        return True
    return job is None


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


async def _record_success(session: AsyncSession, cloud_job: CloudJob, name: str, tally: dict[str, int]) -> None:
    """Succeeded Job: record SUCCEEDED + COMMIT, THEN delete the Job (D-04). No S3 delete, no result.

    The analysis result already landed via the ``/api/internal/agent/*`` callback (KSUBMIT-03), which
    also deleted the staged S3 object inline (D-05) -- so the success path makes ZERO S3 calls and
    NEVER writes an analysis result. Recording + committing before the delete means the status read can
    never lose to GC.
    """
    cloud_job.status = CloudJobStatus.SUCCEEDED.value
    cloud_job.inadmissible = False  # CR-01: a transiently-Inadmissible row that then succeeds must clear the alert flag.
    await session.commit()
    await kube_staging.delete_job(name)
    tally["succeeded"] += 1


async def _handle_no_callback_terminal(
    ctx: dict[str, Any],
    session: AsyncSession,
    cloud_job: CloudJob,
    name: str,
    cap: int,
    tally: dict[str, int],
) -> None:
    """Failed/Evicted (no-callback terminal): bounded re-drive under cap, ANALYSIS_FAILED at cap (D-08).

    At cap (``attempts + 1 > cloud_submit_max_attempts``) the ordering is the load-bearing terminal
    sequence (D-04): record FAILED + FileRecord ANALYSIS_FAILED + COMMIT -> ``delete_staged_object``
    (D-05) -> ``delete_job``. There is NO cross-target fallback (KSUBMIT-05).

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
        cloud_job.status = CloudJobStatus.FAILED.value
        cloud_job.inadmissible = False  # CR-01: terminal row must not keep the operator alert lit.
        await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYSIS_FAILED))
        await session.commit()
        await s3_staging.delete_staged_object(file_id)
        await kube_staging.delete_job(name)
        tally["failed"] += 1
        logger.warning("reconcile_cloud_jobs: submit cap reached -> ANALYSIS_FAILED", file_id=str(file_id), attempt=next_attempt, cap=cap)
        return

    # Under cap -> re-drive. Delete the prior Job, then confirm it is gone before re-submitting.
    await kube_staging.delete_job(name)
    if not await _job_gone(name):
        logger.info("reconcile_cloud_jobs: prior Job still terminating; deferring re-drive", file_id=str(file_id), kueue_workload=name)
        return
    cloud_job.attempts = next_attempt
    cloud_job.status = CloudJobStatus.SUBMITTED.value
    cloud_job.inadmissible = False  # CR-01: re-driving a failed Job clears any stale Inadmissible flag.
    await session.commit()
    await _enqueue_resubmit(ctx, file_id)
    tally["redriven"] += 1
    logger.info("reconcile_cloud_jobs: re-driving submit_cloud_job", file_id=str(file_id), attempt=next_attempt)


async def _reconcile_one(ctx: dict[str, Any], session: AsyncSession, cloud_job: CloudJob, cap: int, tally: dict[str, int]) -> None:
    """Reconcile a single in-flight ``cloud_job`` row against its Job + Kueue Workload."""
    name = cloud_job.kueue_workload
    if not name:
        logger.warning("reconcile_cloud_jobs: cloud_job missing kueue_workload; skipping", cloud_job_id=str(cloud_job.id))
        return

    # WR-01: a vanished Job (real kube 404 -> NotFoundError; fake seam -> None) on an in-flight row is a
    # no-callback terminal, NOT a transient error. Route it to the bounded re-drive / ANALYSIS_FAILED
    # handler instead of letting NotFoundError bubble to the per-row guard, where it would be rolled back
    # and skipped every tick -- leaving the row stuck in-flight forever (e.g. a Failed Job GC'd by
    # ttlSecondsAfterFinished before reconcile read it, or an enqueue that raised after the attempt commit).
    try:
        job = await kube_staging.get_job(name)
    except kr8s.NotFoundError:
        job = None
    if job is None:
        await _handle_no_callback_terminal(ctx, session, cloud_job, name, cap, tally)
        return

    # 1. Job terminal signals first -- the Job is the source of truth for succeeded-vs-failed.
    if _job_counter(job, "succeeded") >= 1 or _job_has_true_condition(job, "Complete"):
        await _record_success(session, cloud_job, name, tally)
        return
    if _job_counter(job, "failed") >= 1 or _job_has_true_condition(job, "Failed"):
        await _handle_no_callback_terminal(ctx, session, cloud_job, name, cap, tally)
        return

    # 2. Not terminal -> read the paired Kueue Workload for admission state (D-02 by job-uid).
    uid = str(getattr(getattr(job, "metadata", None), "uid", "") or "")
    workload = await kube_staging.get_workload_for(uid) if uid else None
    if workload is None:
        # Admission state unreadable this tick (label miss + owner-ref miss) -> stay in-flight, no-op.
        return

    # Evicted/deactivated -> no-callback terminal (re-drive under cap).
    evicted = _workload_condition(workload, _TYPE_EVICTED)
    if evicted is not None and evicted.get("status") == "True":
        await _handle_no_callback_terminal(ctx, session, cloud_job, name, cap, tally)
        return

    quota_reserved = _workload_condition(workload, _TYPE_QUOTA_RESERVED)

    # Inadmissible (operator misconfig): loud + hold, NEVER consumes the cap (D-06/D-07).
    if quota_reserved is not None and quota_reserved.get("status") == "False" and quota_reserved.get("reason") == _REASON_INADMISSIBLE:
        if not cloud_job.inadmissible:
            cloud_job.inadmissible = True
            await session.commit()
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
            await session.commit()
        tally["pending"] += 1
        return

    # Admitted / QuotaReserved=True -> in-flight running; advance SUBMITTED -> RUNNING.
    admitted = _workload_condition(workload, _TYPE_ADMITTED)
    if (admitted is not None and admitted.get("status") == "True") or (quota_reserved is not None and quota_reserved.get("status") == "True"):
        if cloud_job.status != CloudJobStatus.RUNNING.value or cloud_job.inadmissible:
            cloud_job.status = CloudJobStatus.RUNNING.value
            cloud_job.inadmissible = False  # CR-01: an admitted Workload is no longer Inadmissible -- clear the alert.
            await session.commit()
        tally["running"] += 1
        return

    # Unknown in-flight condition set -> leave the row untouched for a later tick.


async def reconcile_cloud_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Reconcile every in-flight ``cloud_job`` against its Kueue Job/Workload; return a tally dict.

    The ``*/5`` cron body (D-01/D-03). Iterates ``cloud_job`` rows in SUBMITTED/RUNNING (D-02), maps
    each Job + Workload condition set to an outcome, enforces the delete-after-record ordering + S3
    cleanup (D-04/D-05), drives the bounded re-drive to ANALYSIS_FAILED (D-08), surfaces Inadmissible
    without consuming the cap (D-06/D-07), and NEVER writes an analysis result (KSUBMIT-03). Each row is
    guarded so one bad row never aborts the tick (the cron no-op discipline).
    """
    cfg = get_settings()
    cap = cfg.cloud_submit_max_attempts  # type: ignore[attr-defined]
    tally = {"reconciled": 0, "succeeded": 0, "failed": 0, "redriven": 0, "inadmissible": 0, "pending": 0, "running": 0}

    async with ctx["async_session"]() as session:
        rows = (
            (await session.execute(select(CloudJob).where(CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]))))
            .scalars()
            .all()
        )
        # Capture the primitive ids while the rows are live -- the per-row guard's ``session.rollback``
        # expires the ORM identity map, so we re-fetch each row fresh (async) inside the loop rather
        # than touching a stale/expired ORM object after a sibling row rolled back.
        cloud_job_ids = [row.id for row in rows]

        for cloud_job_id in cloud_job_ids:
            try:
                cloud_job = await session.get(CloudJob, cloud_job_id)
                if cloud_job is None:
                    continue
                # IN-01: count only rows that actually reach reconcile (a concurrently deleted/terminalized
                # row resolves to None above and must not inflate the log-only tally).
                tally["reconciled"] += 1
                await _reconcile_one(ctx, session, cloud_job, cap, tally)
            except Exception:
                # Per-row guard: a single bad row (transient kube error, unexpected shape) never aborts
                # the tick. Roll back any partial mutation so the session stays usable for the next row.
                await session.rollback()
                logger.warning("reconcile_cloud_jobs: row reconcile failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)

    logger.info("reconcile_cloud_jobs complete", **tally)
    return tally
