"""Control-plane fast Kube-submit producer (Phase 54, Plan 05 -- KSUBMIT-01/02/06).

``submit_cloud_job`` is the FAST controller-queue task: ONE kube POST (via
``kube_staging.submit_job``) that returns in seconds, upserts the ``cloud_job`` row
(``status=SUBMITTED`` + the Kueue Job name in ``kueue_workload``), commits, and returns. It NEVER
awaits analysis and writes no analysis-result row -- the one-shot pod runs the analysis and PUTs the
result back through the existing ``/api/internal/agent/analysis/{file_id}`` callback (KSUBMIT-02).

THREE load-bearing invariants:

* **Idempotent** (KSUBMIT-01): the deterministic Job name ``phaze-analyze-<file_id>`` means a
  duplicate submit hits a 409 AlreadyExists -- swallowed inside the ``kube_staging.submit_job`` seam
  (409 -> refresh), so the task stays thin. The ``cloud_job`` upsert ``ON CONFLICT (file_id)`` keeps
  exactly one row, so a re-submit produces neither a duplicate Job nor a duplicate row.
* **No-ledger-seed** (KSUBMIT-06, the CLOUDROUTE-02 hazard): the submit path writes ONLY the
  ``cloud_job`` row. It NEVER imports or writes a scheduling-ledger ``process_file:<id>`` row --
  such a row would let ``reenqueue.recover_orphaned_work`` replay the K8s file onto a LOCAL agent
  queue. The ``cloud_job`` row is the in-flight registry the reconcile cron iterates (D-02).
* **Control-only**: reads ``ctx["async_session"]`` (controller worker shape, like
  ``recover_orphaned_work`` / ``stage_cloud_window``) and the kube surface via ``kube_staging`` --
  kube credentials live on the control plane only (DIST-01). Registered in
  ``enqueue_router.CONTROLLER_TASKS`` + ``controller.settings["functions"]``. It is not enqueued
  directly by ``stage_cloud_window`` (that cron only drives the S3-staging upload leg via
  ``KueueBackend.dispatch``); the live trigger is the S3 upload-complete callback
  (``routers.agent_s3.report_uploaded``), which routes ``submit_cloud_job`` onto the controller
  queue once the agent finishes PUTting the object.

Mirrors the ``cloud_staging.stage_file_to_s3`` producer discipline (``__future__`` annotations, the
``pg_insert(...).on_conflict_do_update`` upsert idiom with the PK stamped OUT of ``set_``, structlog).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import CursorResult, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.services import kube_staging, s3_staging


if TYPE_CHECKING:
    from phaze.config import ControlSettings
    from phaze.config_backends import KubeConfig


logger = structlog.get_logger(__name__)


def _resolve_backend_kube(settings: ControlSettings, backend_id: str | None) -> KubeConfig:
    """Resolve THIS file's owning kueue backend ``KubeConfig`` from ``cloud_job.backend_id`` (MKUE-01).

    The drain stamped ``cloud_job.backend_id`` at dispatch, so a submit resolves its target cluster from
    the recorded id -- NEVER a module-global ``active_kube``. A submit with no ``cloud_job`` row / no
    matching kueue backend is a misconfiguration: fail loud (``KubeStagingError``) rather than POST to an
    arbitrary cluster.
    """
    if backend_id:
        for entry in settings.backends:
            if entry.id == backend_id and entry.kind == "kueue" and getattr(entry, "kube", None) is not None:
                return cast("KubeConfig", entry.kube)
    raise kube_staging.KubeStagingError(
        f"submit_cloud_job: no kueue backend resolves cloud_job.backend_id={backend_id!r} (a submit with no owning backend is a misconfiguration)"
    )


def submit_cloud_job_key(file_id: uuid.UUID) -> str:
    """Return the deterministic SAQ job key ``submit_cloud_job:<file_id>`` for a submit.

    Mirrors ``cloud_staging``'s ``s3_upload:<file_id>`` and ``release_awaiting_cloud``'s
    ``push_file:<file_id>``: a double enqueue of an already-submitting file dedups to a no-op via
    SAQ's per-queue incomplete set. ``file_id`` is a server-generated UUID -- no untrusted free-text
    enters the key. ``routers.agent_s3.report_uploaded`` (the live trigger) and
    ``reconcile_cloud_jobs``'s re-drive path both use this when they enqueue ``submit_cloud_job``.
    """
    return f"submit_cloud_job:{file_id}"


async def submit_cloud_job(ctx: dict[str, Any], file_id: str | uuid.UUID) -> dict[str, str]:
    """Submit the suspended Kube Job for ``file_id`` and upsert its ``cloud_job`` row (KSUBMIT-01/02/06).

    One fast kube POST (``kube_staging.submit_job``; the 409->refresh idempotency lives in the seam)
    then an idempotent ``cloud_job`` upsert ``ON CONFLICT (file_id)`` flipping the row to
    ``SUBMITTED`` and stamping the Kueue Job name into ``kueue_workload``. Writes ONLY the
    ``cloud_job`` row -- NO scheduling-ledger seed (KSUBMIT-06) and no analysis result
    (KSUBMIT-02). Returns ``{"file_id": ..., "kueue_workload": ...}``.

    ``file_id`` arrives as a JSON string over SAQ (or a ``uuid.UUID`` from a direct/test caller); it
    is coerced to ``uuid.UUID`` once so the seam + ORM see a real UUID.
    """
    fid = file_id if isinstance(file_id, uuid.UUID) else uuid.UUID(str(file_id))
    cfg = cast("ControlSettings", get_settings())

    # MKUE-01: resolve THIS file's owning backend cluster from the recorded cloud_job.backend_id BEFORE
    # the POST (the drain stamped it at dispatch). Read it in a short session, resolve the KubeConfig,
    # then POST outside the txn so no DB connection is held across the kube call.
    async with ctx["async_session"]() as session:
        backend_id = (await session.execute(select(CloudJob.backend_id).where(CloudJob.file_id == fid))).scalar_one_or_none()
    kube = _resolve_backend_kube(cfg, backend_id)

    # One fast kube POST against the resolved cluster. The deterministic name + 409->refresh inside the
    # seam makes a duplicate submit safe (no duplicate Job); on a non-409 server error KubeStagingError
    # surfaces and nothing is written below -- a clean retry leaves no orphan cloud_job row.
    name, _uid = await kube_staging.submit_job(fid, kube)

    async with ctx["async_session"]() as session:
        # Idempotent upsert against the unique file_id FK (mirrors the cloud_staging / scheduling_ledger
        # upsert idiom). The s3_key is required NOT NULL on INSERT (a first submit with no prior S3
        # staging row); it is the same file_id-scoped key the upload leg uses. On conflict we refresh
        # ONLY status + kueue_workload -- the PK and s3_key are immutable for the file.
        stmt = pg_insert(CloudJob).values(
            # Stamp the PK explicitly (CR-01 defensive: the single-row kwargs form applies the
            # Python-side default today, but the list/multi-values form would not).
            id=uuid.uuid4(),
            file_id=fid,
            s3_key=s3_staging.staged_object_key(fid),
            status=CloudJobStatus.SUBMITTED.value,
            kueue_workload=name,
            # Seed the Kueue admission progression at the start of the line (D-04). A re-submit resets
            # it (below) so a re-driven Job starts from queued_behind_quota again.
            cloud_phase=CloudPhase.QUEUED_BEHIND_QUOTA.value,
        )
        stmt = stmt.on_conflict_do_update(
            # id + s3_key intentionally OUT of set_: both are immutable identity for the file, so an
            # existing row keeps them on a re-submit (only status/kueue_workload/cloud_phase refresh).
            index_elements=["file_id"],
            set_={
                "status": stmt.excluded.status,
                "kueue_workload": stmt.excluded.kueue_workload,
                "cloud_phase": stmt.excluded.cloud_phase,
                # TimestampMixin.updated_at's ORM onupdate=func.now() never fires on this Core ON
                # CONFLICT DO UPDATE path -- stamp it explicitly so a re-submit bumps updated_at
                # instead of freezing it at first write (phaze-c8nz). created_at stays pinned. The
                # CAS `where=` predicate below is unchanged by this addition.
                "updated_at": func.now(),
            },
            # phaze-kzto: guard the conflict update on the CURRENT status. Every other status writer in
            # this pipeline uses a CAS to stop late/duplicate writers; this upsert did not. A delayed
            # submit (e.g. the controller queue backlogged past a reconcile tick that already spilled
            # the file to 'awaiting' or finalized it 'succeeded') would otherwise RESURRECT that row to
            # SUBMITTED, re-inflate the kueue in_flight cap with a phantom slot, and create a doomed
            # duplicate Job (whose staged S3 input was already deleted). Only an UPLOADED (the normal
            # post-staging status) or SUBMITTED (an idempotent re-run) row may advance. A first-time
            # submit with no prior row still INSERTs -- there is no conflict, so this WHERE never applies.
            where=CloudJob.status.in_((CloudJobStatus.UPLOADED.value, CloudJobStatus.SUBMITTED.value)),
        )
        res = cast("CursorResult[Any]", await session.execute(stmt))
        if res.rowcount == 0:
            # The conflict row is in a non-advanceable status (spilled 'awaiting' / terminal). This is a
            # late/duplicate submit: do NOT flip the row, and tear down the Job we just POSTed so no
            # orphaned doomed pod (its S3 input may already be deleted) lingers or charges a cap slot.
            await session.rollback()
            await kube_staging.delete_job(name, kube)
            logger.warning(
                "submit_cloud_job: late/duplicate submit no-op (cloud_job not uploaded/submitted); deleted Job",
                file_id=str(fid),
                kueue_workload=name,
            )
            return {"file_id": str(fid), "kueue_workload": name, "status": "skipped"}
        await session.commit()

    logger.info("submit_cloud_job: cloud_job submitted", file_id=str(fid), kueue_workload=name)
    return {"file_id": str(fid), "kueue_workload": name}
