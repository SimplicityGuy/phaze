"""Control-plane cloud-staging producer + re-drive helper (Phase 53, Plan 04 -- KSTAGE-01).

The control-side orchestration of the S3 object-staging upload leg. ``stage_file_to_s3`` is the
*upload-trigger seam*: in one transaction it creates the ``cloud_job`` row, initiates the
multipart upload, presigns the part URLs, and enqueues exactly one ``s3_upload`` job through the
single per-agent enqueue seam (``select_active_agent`` + ``task_router.queue_for`` --- the Phase 30
invariant that no producer routes onto the consumer-less default queue). The file-server agent
then PUTs the bytes to those presigned URLs; the control plane never touches file bytes (DIST-01).

D-01: a presigned MULTIPART upload (not a single PUT) so the agent streams one bounded part at a
time and the control plane completes the object itself. The producer is built + unit-tested here
but is NOT wired into the live cloud-window routing seam --- Phase 55 owns that routing decision;
this module provides the seam it will call.

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
from phaze.services.enqueue_router import select_active_agent
from phaze.tasks.s3_upload import UPLOAD_FILE_SAQ_TIMEOUT_SEC


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


async def stage_file_to_s3(session: AsyncSession, file: FileRecord, task_router: AgentTaskRouter) -> None:
    """Stage ``file`` to S3 and enqueue its upload (the upload-trigger seam, KSTAGE-01/D-01).

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
    5. Commit.
    """
    cfg = cast("ControlSettings", get_settings())

    # Gate on an online fileserver agent BEFORE mutating anything: with none available this is a
    # clean hold (NoActiveAgentError propagates) -- no multipart, no cloud_job, no enqueue.
    agent = await select_active_agent(session, kind="fileserver")

    upload_id = await s3_staging.create_multipart_upload(file.id)
    part_count = max(1, math.ceil(file.file_size / cfg.s3_multipart_part_size_bytes))
    part_urls = await s3_staging.presign_upload_parts(file.id, upload_id, part_count)

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
    )
    stmt = stmt.on_conflict_do_update(
        # id is intentionally OUT of set_: the PK is immutable, so an existing row keeps its id on a
        # re-stage (only the key/status/upload_id refresh).
        index_elements=["file_id"],
        set_={
            "s3_key": stmt.excluded.s3_key,
            "status": stmt.excluded.status,
            "upload_id": stmt.excluded.upload_id,
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
    queue = task_router.queue_for(agent.id)
    await queue.connect()
    await queue.enqueue(
        "s3_upload",
        key=f"s3_upload:{file.id}",
        timeout=UPLOAD_FILE_SAQ_TIMEOUT_SEC,
        **payload.model_dump(mode="json"),
    )
    await session.commit()

    logger.info(
        "stage_file_to_s3: cloud_job staged + s3_upload enqueued",
        file_id=str(file.id),
        agent_id=agent.id,
        part_count=part_count,
    )


async def redrive_upload(session: AsyncSession, file: FileRecord, task_router: AgentTaskRouter) -> None:
    """Abort the prior multipart (best-effort) and re-stage ``file`` with a fresh upload.

    Used by the Plan-04 ``/failed`` callback under the re-drive cap: a failed/abandoned upload
    leaves an in-flight multipart that must be aborted before a new one is initiated, so the prior
    attempt's bytes do not linger. The abort is best-effort (the upload may already be gone --- an
    eviction, a prior abort, a lifecycle sweep), so its failure never blocks the re-stage.
    """
    existing = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    if existing is not None and existing.upload_id:
        # Best-effort cleanup: the multipart may already be gone (eviction / prior abort / lifecycle
        # sweep), so any failure here must not block the re-stage below.
        with contextlib.suppress(Exception):
            await s3_staging.abort_multipart_upload(file.id, existing.upload_id)
    await stage_file_to_s3(session, file, task_router)
