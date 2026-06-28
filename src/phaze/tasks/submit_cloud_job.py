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
  ``enqueue_router.CONTROLLER_TASKS`` + ``controller.settings["functions"]``; NOT wired into the
  live ``stage_cloud_window`` trigger here (Phase 55 owns that).

Mirrors the ``cloud_staging.stage_file_to_s3`` producer discipline (``__future__`` annotations, the
``pg_insert(...).on_conflict_do_update`` upsert idiom with the PK stamped OUT of ``set_``, structlog).
"""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services import kube_staging, s3_staging


logger = structlog.get_logger(__name__)


def submit_cloud_job_key(file_id: uuid.UUID) -> str:
    """Return the deterministic SAQ job key ``submit_cloud_job:<file_id>`` for a submit.

    Mirrors ``cloud_staging``'s ``s3_upload:<file_id>`` and ``release_awaiting_cloud``'s
    ``push_file:<file_id>``: a double enqueue of an already-submitting file dedups to a no-op via
    SAQ's per-queue incomplete set. ``file_id`` is a server-generated UUID -- no untrusted free-text
    enters the key. Phase 55 (the trigger owner) uses this when it enqueues ``submit_cloud_job``.
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

    # One fast kube POST. The deterministic name + 409->refresh inside the seam makes a duplicate
    # submit safe (no duplicate Job); on a non-409 server error KubeStagingError surfaces and nothing
    # is written below -- a clean retry leaves no orphan cloud_job row.
    name, _uid = await kube_staging.submit_job(fid)

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
        )
        stmt = stmt.on_conflict_do_update(
            # id + s3_key intentionally OUT of set_: both are immutable identity for the file, so an
            # existing row keeps them on a re-submit (only status/kueue_workload refresh).
            index_elements=["file_id"],
            set_={
                "status": stmt.excluded.status,
                "kueue_workload": stmt.excluded.kueue_workload,
            },
        )
        await session.execute(stmt)
        await session.commit()

    logger.info("submit_cloud_job: cloud_job submitted", file_id=str(fid), kueue_workload=name)
    return {"file_id": str(fid), "kueue_workload": name}
