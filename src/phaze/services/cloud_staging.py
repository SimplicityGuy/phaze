"""Control-plane cloud-staging producer + re-drive helper (Phase 53, Plan 04 -- KSTAGE-01).

The control-side orchestration of the S3 object-staging upload leg. ``stage_file_to_s3`` is the
*upload-trigger seam*: in one transaction it creates the ``cloud_job`` row, initiates the
multipart upload, presigns the part URLs, and enqueues exactly one ``s3_upload`` job through the
single per-agent enqueue seam (``select_active_agent`` + ``task_router.queue_for`` --- the Phase 30
invariant that no producer routes onto the consumer-less default queue). The file-server agent
then PUTs the bytes to those presigned URLs; the control plane never touches file bytes (DIST-01).

D-01: a presigned MULTIPART upload (not a single PUT) so the agent streams one bounded part at a
time and the control plane completes the object itself. The producer built here is wired into the
live cloud-window routing seam via ``KueueBackend.dispatch`` (``phaze.services.backends``), which
calls the no-commit ``_stage_file_to_s3`` core per candidate under the drain's advisory lock.

Mirrors the ``agent_push.py`` producer idiom (queue_for -> connect -> enqueue with an explicit SAQ
job-net timeout + a deterministic key) and the stateless-service conventions of ``enqueue_router``
/ ``s3_staging`` (module-level async functions, ``__future__`` annotations, ``TYPE_CHECKING``
guard). All S3 SDK calls are delegated to ``s3_staging`` (the single SDK home); this module holds
the ORM + queue orchestration only.
"""

from __future__ import annotations

import contextlib
import math
from typing import TYPE_CHECKING, cast
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.schemas.agent_s3 import UploadFileS3Payload
from phaze.services import s3_staging
from phaze.services.enqueue_router import lane_for_task, select_active_agent
from phaze.tasks.s3_upload import UPLOAD_FILE_SAQ_TIMEOUT_SEC


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BucketConfig
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


async def stage_file_to_s3(session: AsyncSession, file: FileRecord, task_router: AgentTaskRouter, bucket: BucketConfig) -> None:
    """Stage ``file`` to ``bucket`` and enqueue its upload, COMMITTING (upload-trigger seam, KSTAGE-01/D-01).

    Thin committing wrapper around :func:`_stage_file_to_s3`: it runs the full staging body then
    commits once. This is the form ``redrive_upload`` (``cloud_staging.py``) calls -- it owns its own
    single-file transaction, so the commit belongs here.

    The bounded ``stage_cloud_window`` cron (Phase 55, KROUTE-02) instead calls the no-commit
    :func:`_stage_file_to_s3` core PER CANDIDATE inside its advisory-locked loop and commits ONCE
    after the loop -- a per-candidate commit here would release ``pg_advisory_xact_lock`` mid-loop
    and re-open the over-stage class (Landmine L1). The two callers share the body; only the commit
    boundary differs.

    ``bucket`` is the D-06 per-file staging bucket the ``KueueBackend.dispatch`` caller picked; it is
    threaded into the S3 SDK calls AND recorded on ``cloud_job.staging_bucket`` (MKUE-02).

    Steps (mirroring the ``agent_push`` producer idiom): see :func:`_stage_file_to_s3`.
    """
    await _stage_file_to_s3(session, file, task_router, bucket)
    await session.commit()


async def _stage_file_to_s3(session: AsyncSession, file: FileRecord, task_router: AgentTaskRouter, bucket: BucketConfig) -> None:
    """Run the full S3-staging body WITHOUT committing (Landmine L1: the no-commit core).

    Identical to the public :func:`stage_file_to_s3` minus the terminal ``session.commit()`` so the
    caller owns the transaction boundary. Used per-candidate by the advisory-locked
    ``stage_cloud_window`` cron loop, which commits ONCE after the loop -- so the ``pg_advisory_xact_lock``
    is held across the whole tick and the ≤N window can never be over-staged.

    Steps (mirroring the ``agent_push`` producer idiom):

    1. Resolve the active FILESERVER agent (it owns the media mount and runs the upload). A
       :class:`NoActiveAgentError` is allowed to propagate for a clean hold --- nothing is written,
       so the caller (Phase 55 / a re-drive) can retry once an agent appears.
    2. Initiate the multipart upload and presign ``part_count = ceil(file_size / part_size)`` (min
       1) PUT URLs via ``s3_staging`` (the only S3-SDK home).
    3. Upsert the ``cloud_job`` row (``UPLOADING`` + file_id-scoped key + multipart ``upload_id``)
       ON CONFLICT (file_id) so a re-stage is idempotent against the unique FK (no duplicate row).
    4. Enqueue exactly one ``s3_upload`` job on the agent's queue carrying the presigned part URLs,
       the part size, and the file_id, with the deterministic ``s3_upload:<file_id>`` key and the
       explicit ``UPLOAD_FILE_SAQ_TIMEOUT_SEC`` job-net timeout (WR-03).

    phaze-uciu.3: step 3 (the ``cloud_job`` upsert) and step 4 (the enqueue) run inside a
    ``session.begin_nested()`` SAVEPOINT. SAQ's ``PostgresQueue`` enqueue uses its OWN psycopg pool,
    so a ``queue.connect()``/``queue.enqueue()`` failure raises WITHOUT poisoning this asyncpg
    session -- a bare (un-savepointed) upsert-then-raise would leave the ``status='uploading'`` row
    intact for the drain's post-loop commit: a stranded row (unrecoverable -- reconcile/orphan-
    recovery both scope away from in-flight cloud_jobs) that permanently consumes an
    ``in_flight_count`` cap slot. The SAVEPOINT rolls back ONLY the upsert on a raise, restoring the
    row's prior state (typically ``status='awaiting'``, D-01) while the outer transaction (and its
    ``pg_advisory_xact_lock``, when called from the drain) stays alive. The raise itself still
    propagates to the caller (``KueueBackend.dispatch`` / the drain's per-candidate ``except``).
    """
    cfg = cast("ControlSettings", get_settings())

    # Gate on an online fileserver agent BEFORE mutating anything: with none available this is a
    # clean hold (NoActiveAgentError propagates) -- no multipart, no cloud_job, no enqueue.
    agent = await select_active_agent(session, kind="fileserver")

    upload_id = await s3_staging.create_multipart_upload(file.id, bucket)
    part_count = max(1, math.ceil(file.file_size / cfg.s3_multipart_part_size_bytes))
    part_urls = await s3_staging.presign_upload_parts(file.id, upload_id, part_count, bucket)

    async with session.begin_nested():
        # Idempotent upsert against the unique file_id FK: a re-stage refreshes the key/status/upload_id
        # in place instead of erroring on the duplicate (mirrors the scheduling_ledger upsert idiom).
        stmt = pg_insert(CloudJob).values(
            # Stamp the PK explicitly: the single-row kwargs form of pg_insert DOES apply CloudJob.id's
            # Python-side default=uuid.uuid4 today (verified against real Postgres), but the list/multi-
            # values form does NOT -- mirror the agent_analysis.py AnalysisResult precedent so a future
            # conversion to that form cannot regress into a NOT NULL violation (CR-01, defensive).
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=s3_staging.staged_object_key(file.id),
            status=CloudJobStatus.UPLOADING.value,
            upload_id=upload_id,
            # D-01/D-06 (MKUE-02): record WHICH bucket staged this file's object, authoritatively, so
            # presign/cleanup READ this column and never re-derive via pick_bucket (config-set drift-safe).
            staging_bucket=bucket.id,
        )
        stmt = stmt.on_conflict_do_update(
            # id is intentionally OUT of set_: the PK is immutable, so an existing row keeps its id on a
            # re-stage (only the key/status/upload_id/staging_bucket refresh).
            index_elements=["file_id"],
            set_={
                "s3_key": stmt.excluded.s3_key,
                "status": stmt.excluded.status,
                "upload_id": stmt.excluded.upload_id,
                "staging_bucket": stmt.excluded.staging_bucket,
            },
        )
        await session.execute(stmt)

        payload = UploadFileS3Payload(
            file_id=file.id,
            original_path=file.original_path,
            part_urls=part_urls,
            part_size_bytes=cfg.s3_multipart_part_size_bytes,
            agent_id=agent.id,
        )
        queue = task_router.queue_for(agent.id, lane_for_task("s3_upload"))
        await queue.connect()
        await queue.enqueue(
            "s3_upload",
            key=f"s3_upload:{file.id}",
            timeout=UPLOAD_FILE_SAQ_TIMEOUT_SEC,
            **payload.model_dump(mode="json"),
        )

    logger.info(
        "stage_file_to_s3: cloud_job staged + s3_upload enqueued",
        file_id=str(file.id),
        agent_id=agent.id,
        part_count=part_count,
    )


def _redrive_bucket(cfg: ControlSettings, existing: CloudJob | None, file: FileRecord) -> BucketConfig | None:
    """Resolve the bucket a re-drive stages into: the RECORDED one, else a re-pick over the backend's set.

    A re-drive re-stages a file that already carries a ``cloud_job`` row, so its authoritative bucket is
    the recorded ``staging_bucket`` (MKUE-02 -- read it, never re-derive). Only when that column is absent
    (a legacy row staged before Phase 70, or a row whose backend later cleared it) does it fall back to
    re-picking deterministically over the file's backend's bound bucket set -- keeping the fresh multipart
    on the same D-06 bucket the presign/cleanup path will read.
    """
    if existing is not None and existing.staging_bucket:
        return s3_staging.resolve_bucket_config(cfg, existing.staging_bucket)
    if existing is not None and existing.backend_id:
        backend = next((b for b in cfg.backends if b.id == existing.backend_id and getattr(b, "buckets", None)), None)
        if backend is not None:
            bucket_ids = list(getattr(backend, "buckets", []) or [])
            if bucket_ids:
                return s3_staging.resolve_bucket_config(cfg, s3_staging.pick_bucket(file.id, bucket_ids))
    return None


async def redrive_upload(session: AsyncSession, file: FileRecord, task_router: AgentTaskRouter) -> None:
    """Abort the prior multipart (best-effort) and re-stage ``file`` with a fresh upload.

    Used by the Plan-04 ``/failed`` callback under the re-drive cap: a failed/abandoned upload
    leaves an in-flight multipart that must be aborted before a new one is initiated, so the prior
    attempt's bytes do not linger. The abort is best-effort (the upload may already be gone --- an
    eviction, a prior abort, a lifecycle sweep), so its failure never blocks the re-stage.

    Both the abort and the re-stage act on the RECORDED ``staging_bucket`` (MKUE-02) so the fresh
    multipart lands on exactly the bucket the presign/cleanup path reads back.
    """
    cfg = cast("ControlSettings", get_settings())
    existing = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    bucket = _redrive_bucket(cfg, existing, file)
    if bucket is None:
        raise s3_staging.S3StagingError(f"redrive_upload could not resolve a staging bucket for {file.id}")
    if existing is not None and existing.upload_id:
        # Best-effort cleanup: the multipart may already be gone (eviction / prior abort / lifecycle
        # sweep), so any failure here must not block the re-stage below.
        with contextlib.suppress(Exception):
            await s3_staging.abort_multipart_upload(file.id, existing.upload_id, bucket)
    await stage_file_to_s3(session, file, task_router, bucket)
