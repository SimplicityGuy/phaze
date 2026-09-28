"""Reap ``cloud_job`` rows whose ``backend_id`` no longer names any configured backend (phaze-pnt12).

WHY THIS EXISTS
---------------
Every reconcile / stranded-row reaper under :mod:`phaze.services.backends` is scoped to ONE resolved
backend: ``KueueBackend._reap_stranded_staging`` and ``ComputeAgentBackend._reap_stranded_submitted``
(both driven from ``reconcile_cloud_jobs``, itself iterating ``resolve_backends(settings)``) filter
``CloudJob.backend_id == self.id`` -- and ``self`` only exists for an id the CURRENT
``[[backends]]`` config still resolves. The moment an operator removes a backend entry, every
``cloud_job`` row still carrying its ``backend_id`` becomes invisible to EVERY reconcile loop at once:
there is no ``self`` left to claim it, and nothing fails loudly to say so.

Measured in production (spike phaze-tch4d, 2026-09-27): 35 rows (32 ``uploaded``, 3 ``uploading``)
carrying ``backend_id='xenolab'`` -- a backend retired well before any of the above reapers existed --
stuck since 2026-08-08, ~7 weeks. Because ``cloud_job`` is one-row-per-``file_id``
(``uq_cloud_job_file_id``) and :func:`phaze.services.pipeline.cloud.get_cloud_staging_candidates` only
ever offers ``status='awaiting'`` rows, those 35 files could NEVER be re-staged to ANY backend until
their row left :data:`~phaze.services.backends.base.IN_FLIGHT` -- permanently parked, and invisible to
``LocalBackend.in_flight_count`` too (that count is ``backend_id``-scoped, so an orphaned row was never
even counted against any live lane's cap).

THE RULE (``get_why`` on this exact question returns no existing decision record -- see the bead
comment trail; the closest precedent is followed as far as the two populations allow):

* **STAGING rows (`uploading` / `uploaded`) are RE-QUEUED.** These are pure S3-side objects whose only
  liveness signal is a live SAQ broker job (``s3_upload:<file_id>`` / ``submit_cloud_job:<file_id>``) --
  a signal entirely orthogonal to whether the backend is still configured. So they get the same general
  shape ``KueueBackend._reap_stranded_staging`` already gives ITS OWN rows: an age bound (the existing
  ``cloud_uploading_stale_after_sec`` / ``cloud_uploaded_stale_after_sec`` settings), a broker-liveness
  gate checked BEFORE it, and a CAS'd spill back to ``status='awaiting'`` via the single go-forward
  writer :func:`~phaze.services.backends.admission.hold_awaiting_cloud` -- releasing the file back to
  ``select_backend`` on the next drain tick, to whichever backend is CURRENTLY configured (or to local,
  once/if its cloud attempt budget is spent). Unlike that reaper, this one deliberately does NOT also
  abort the multipart upload or delete the staged S3 object: the retired backend's own bucket
  configuration may itself be gone (the whole premise here is that we no longer trust anything the
  retired registry entry told us), and the codebase already ships a durable, bounded backstop for
  exactly this (``s3_lifecycle_ttl_days``, KSTAGE-04) that reclaims the object on its own schedule
  regardless of whether this reaper ever runs. Skipping the cleanup trades an already-budgeted, bounded
  storage cost for not having to re-derive a bucket from configuration this reaper has no reason to
  trust.

* **SUBMITTED / RUNNING rows are SURFACED, never auto-reaped.** These are Kueue-Job-backed; the ONLY
  authoritative liveness signal is Job/pod state, readable only through the retired backend's own
  cluster config -- which no longer exists once the registry entry is gone. Blind age-based reaping
  here would be a wall-clock kill of possibly-still-executing analysis, which is precisely what
  ``reconcile_cloud_jobs.py``'s phaze-202e note forbids ("NO WALL CLOCK MAY KILL A RUN"). A
  compute-backend SUBMITTED row's liveness IS broker-keyed rather than cluster-probed (same shape as
  staging), so it could in principle be reaped just as safely -- but nothing on the row itself records
  WHICH kind of backend produced it once its registry entry is gone, so this reaper cannot tell the two
  apart and, deliberately, treats every non-staging orphan the same conservative way the existing
  Inadmissible state already treats a live operator-misconfiguration case elsewhere in this pipeline:
  hold, alert, never spend the cap. Production currently carries ZERO such rows (the 35 measured are
  all ``uploaded``/``uploading``), so this conservative default is not presently masking anything; if
  a genuine SUBMITTED/RUNNING orphan ever appears, the operator remedy is to hand-verify the file is not
  still executing anywhere (the retired cluster, if still reachable out-of-band) and then either spill
  it manually (``hold_awaiting_cloud``) or re-add the backend id to config, which restores normal
  reconcile coverage for exactly this row.

RUNS AT CONTROLLER STARTUP -- deliberately NOT a periodic CronJob. Two independent reasons, not one:

1. A ``cloud_job.backend_id`` can only become orphaned by an operator editing ``[[backends]]``, and
   that edit only takes effect on a controller restart (``ControlSettings`` is resolved once, cached,
   at process start). Boot is therefore not merely A trigger for this reconcile, it is the ONLY moment
   the condition this reaper exists to catch can newly arise -- exactly the reasoning that already
   makes ``recover_orphaned_work`` (queue-loss detection) a gated boot-only reconcile rather than a
   cron, and the acceptance criterion this bead was filed against ("the existing rows are resolved by
   this rule ON DEPLOY") names the same moment directly.
2. ``tests/analyze/recovery_cloud/orphan_replay/test_controller_startup.py::test_no_auto_advance_cron``
   pins a narrow, enumerated allow-list of the only ``*/5 * * * *`` crons permitted to exist, each
   independently argued as bounded/idempotent/non-general (Phase 50's "no general pipeline auto-advance
   cron" invariant, the same one that keeps ``recover_orphaned_work`` OFF the cron list entirely). This
   reaper's own trigger condition is rarer and more precisely known (a config diff, not a heuristic
   liveness probe) than anything already on that list, so a periodic poll would be strictly less
   correct than boot-gating for no benefit -- the first attempt (see :func:`~phaze.tasks.controller.
   _run_boot_reconcile_with_retry`) already retries past a transient not-ready-yet schema.

Registered in ``phaze.tasks.controller.settings["functions"]`` (so it stays independently callable,
mirroring ``submit_cloud_job``) but WITHOUT a ``CronJob`` entry and WITHOUT
``phaze.services.enqueue_router.CONTROLLER_TASKS`` (mirrors ``recover_orphaned_work`` exactly on both
counts). Deliberately NOT folded into ``reconcile_cloud_jobs``'s per-backend loop either: that loop is
Kueue-cluster-aware (imports ``kube_staging``) and this reaper is not -- it never touches a cluster, so
it needs no ``KubeConfig`` and no bound ``Backend`` instance at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import ColumnElement, select, text
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services.backends.admission import hold_awaiting_cloud
from phaze.services.backends.base import STAGING
from phaze.services.pipeline import get_live_job_keys
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)

# The Kueue-Job-backed half of IN_FLIGHT (base.IN_FLIGHT minus base.STAGING): surfaced only, per the
# module docstring's phaze-202e rationale -- never spilled by this reaper.
_SURFACE_ONLY_STATUSES: tuple[CloudJobStatus, ...] = (CloudJobStatus.SUBMITTED, CloudJobStatus.RUNNING)


def _staging_stale_bound_sec(cfg: ControlSettings, status: str) -> int:
    """Return the staleness bound (seconds) for a staging ``status`` -- mirrors ``KueueBackend``'s own."""
    if status == CloudJobStatus.UPLOADING.value:
        return cfg.cloud_uploading_stale_after_sec
    return cfg.cloud_uploaded_stale_after_sec


def _seconds_since_last_staging_write(cloud_job: CloudJob, now: datetime) -> float:
    """How long ago the row was last written -- identical coercion to ``KueueBackend``'s own helper.

    ``updated_at`` is TIMESTAMP WITHOUT TIME ZONE, so asyncpg hands it back NAIVE; assume-UTC before
    subtracting (mirrors ``kueue._seconds_since_last_staging_write``).
    """
    ref = cloud_job.updated_at or cloud_job.created_at
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (now - ref).total_seconds()


def _orphaned_backend_clause(resolved_ids: set[str]) -> ColumnElement[bool]:
    """Build the ``backend_id`` predicate: non-null AND not one of the currently resolved backend ids.

    A plain ``.notin_(resolved_ids)`` with an EMPTY ``resolved_ids`` is still correct SQL (every non-null
    backend_id is "not in the empty set"), but emits a SQLAlchemy ``SAWarning`` for an empty IN/NOT IN
    list -- so the empty case is spelled without ``notin_`` at all.
    """
    if not resolved_ids:
        return CloudJob.backend_id.is_not(None)
    return CloudJob.backend_id.is_not(None) & CloudJob.backend_id.notin_(resolved_ids)


async def _requeue_orphaned_staging_rows(session: AsyncSession, cfg: ControlSettings, resolved_ids: set[str]) -> int:
    """Spill AGE-STRANDED, backend-orphaned {UPLOADING, UPLOADED} rows back to ``awaiting``. Returns the count spilled.

    Per-row discipline mirrors ``KueueBackend._reap_stranded_staging`` exactly (same advisory lock key,
    same fresh re-read under the lock, same broker-liveness gate before the age bound, same CAS via
    ``hold_awaiting_cloud``) -- see that method's docstring for why each guard is load-bearing. The one
    deliberate omission is the S3 cleanup tail; see this module's docstring for why that is safe here.
    """
    candidate_ids = (
        (
            await session.execute(
                select(CloudJob.id).where(
                    CloudJob.status.in_([status.value for status in STAGING]),
                    _orphaned_backend_clause(resolved_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    if not candidate_ids:
        return 0

    now = datetime.now(UTC)
    # phaze-31q3-style liveness gate: a row whose owning broker key is still queued/active is live work
    # the callback path still owns, regardless of ``updated_at`` age or backend-registry membership.
    live_keys = await get_live_job_keys(session)
    requeued = 0

    for cloud_job_id in candidate_ids:
        try:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
            # populate_existing=True forces a real re-read under the lock -- the snapshot above may be
            # stale by the time this row's lock is acquired (mirrors kueue.py's phaze-7lpb fix).
            cloud_job = await session.get(CloudJob, cloud_job_id, populate_existing=True)
            if cloud_job is None or cloud_job.status not in {status.value for status in STAGING}:
                await session.rollback()
                continue
            observed_status = cloud_job.status
            live_key = (
                f"s3_upload:{cloud_job.file_id}" if observed_status == CloudJobStatus.UPLOADING.value else f"submit_cloud_job:{cloud_job.file_id}"
            )
            if live_key in live_keys:
                await session.rollback()
                continue
            age_sec = _seconds_since_last_staging_write(cloud_job, now)
            bound_sec = _staging_stale_bound_sec(cfg, observed_status)
            if age_sec < bound_sec:
                await session.rollback()
                continue
            file_id = cloud_job.file_id
            upload_id = cloud_job.upload_id
            attempts = min(cloud_job.attempts + 1, cfg.cloud_submit_max_attempts)
            file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
            spilled = file is not None and await hold_awaiting_cloud(
                session,
                file,
                attempts=attempts,
                expect_status=(observed_status,),
                expect_upload_id=upload_id,
                clear_cloud_phase=True,
            )
            if not spilled:
                # Lost the race to a live callback (or the FK file vanished): full no-op.
                await session.rollback()
                continue
            await clear_ledger_entry(session, f"s3_upload:{file_id}")
            if observed_status == CloudJobStatus.UPLOADED.value:
                await clear_ledger_entry(session, f"submit_cloud_job:{file_id}")
            await session.commit()
            requeued += 1
            logger.warning(
                "reap_orphaned_backend_cloud_jobs: backend-orphaned staging row requeued (spilled to awaiting)",
                cloud_job_id=str(cloud_job_id),
                file_id=str(file_id),
                backend_id=cloud_job.backend_id,
                observed_status=observed_status,
                age_sec=int(age_sec),
                bound_sec=bound_sec,
                attempts=attempts,
            )
        except Exception:
            await session.rollback()
            logger.warning("reap_orphaned_backend_cloud_jobs: staging requeue failed; continuing", cloud_job_id=str(cloud_job_id), exc_info=True)

    return requeued


async def _surface_orphaned_inflight_rows(session: AsyncSession, resolved_ids: set[str]) -> int:
    """Log (never mutate) every backend-orphaned {SUBMITTED, RUNNING} row. Returns the count surfaced."""
    rows = (
        (
            await session.execute(
                select(CloudJob).where(
                    CloudJob.status.in_([status.value for status in _SURFACE_ONLY_STATUSES]),
                    _orphaned_backend_clause(resolved_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        logger.warning(
            "reap_orphaned_backend_cloud_jobs: backend-orphaned in-flight row surfaced (NOT auto-reaped -- see module docstring)",
            cloud_job_id=str(row.id),
            file_id=str(row.file_id),
            backend_id=row.backend_id,
            status=row.status,
        )
    return len(rows)


async def reap_orphaned_backend_cloud_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Control-side boot reconcile: requeue/surface ``cloud_job`` rows whose ``backend_id`` names no configured backend.

    CONTROL-ONLY (needs ``ctx["async_session"]``, mirrors every other reaper in this package). Never
    imports ``fastapi``/``phaze.routers`` and never touches ``kube_staging`` -- see the module docstring
    for why this reaper is deliberately cluster-probe-free. Run once at controller startup (see the
    module docstring for why boot, not a cron), gated the same way ``recover_orphaned_work`` is
    (``phaze.tasks.controller._run_boot_reconcile_with_retry``). ``resolve_backends`` is imported
    FUNCTION-LOCALLY (deferred) for the same reason ``reconcile_cloud_jobs.py`` does: ``services.backends``
    is a package this module's own imports (``admission``, ``base``) live under, and a module-top import
    of the package ``__init__`` here would risk the same collection-time cycle.
    """
    from phaze.services.backends import resolve_backends  # noqa: PLC0415 -- deferred, mirrors reconcile_cloud_jobs.reconcile_cloud_jobs

    cfg = cast("ControlSettings", get_settings())
    resolved_ids = {backend.id for backend in resolve_backends(cfg)}

    async with ctx["async_session"]() as session:
        requeued = await _requeue_orphaned_staging_rows(session, cfg, resolved_ids)
        surfaced = await _surface_orphaned_inflight_rows(session, resolved_ids)

    tally = {"requeued": requeued, "surfaced": surfaced}
    logger.info("reap_orphaned_backend_cloud_jobs complete", **tally)
    return tally
