"""POST /api/internal/agent/s3/{file_id}/{uploaded,failed} -- control-side S3-staging callbacks (Phase 53, Plan 04).

The control plane is the only place with the S3 credentials and the ORM, so the Postgres-free,
SDK-free file-server agent reports its multipart-upload outcome through these token-authed internal
callbacks. The control plane then COMPLETES the multipart upload itself (KSTAGE-01/DIST-01 -- never
the agent), flips the ``cloud_job`` state, and runs the bounded re-drive / terminal-cleanup loop.

Mirrors ``agent_push.py`` (report_pushed / report_push_mismatch):

- ``/uploaded`` -- the agent reports the ordered ``(part_number, etag)`` list it collected from each
  part PUT. Control completes the multipart upload control-side, then flips ``cloud_job``
  ``UPLOADING -> UPLOADED`` with a rowcount guard. A duplicate/late callback (cloud_job already
  UPLOADED) is an idempotent 200 that does NOT re-complete the object (T-53-15).

- ``/failed`` -- the agent reports an upload failure. The ``s3_upload_attempt`` counter rides the
  ``s3_upload:<file_id>`` scheduling-ledger payload JSONB (Pitfall 4). Under
  ``push_max_attempts`` control re-drives the upload (``cloud_staging.redrive_upload``: abort the
  prior multipart + re-stage) keeping ``cloud_job`` UPLOADING and stamps the incremented attempt
  back (T-53-16). At/over the cap control sets ``cloud_job`` FAILED, aborts the multipart, deletes
  the staged object, and clears the ledger -- the terminal cleanup that prevents orphaned in-flight
  uploads / leaked objects (KSTAGE-04 / T-53-17). With no fileserver online the re-drive is a clean
  200 hold (NoActiveAgentError caught), never a 500 (T-53-19).

AUTH-01 discipline: ``file_id`` always travels on the URL PATH; the agent identity comes from the
token dependency. The request bodies carry NO identity (``extra="forbid"`` on the schemas).
"""

from typing import TYPE_CHECKING, Annotated, Any, cast
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_s3 import UploadedRequest, UploadedResponse, UploadFailedRequest, UploadFailedResponse
from phaze.services import cloud_staging, s3_staging
from phaze.services.enqueue_router import NoActiveAgentError, resolve_queue_for_task
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks.submit_cloud_job import submit_cloud_job_key


if TYPE_CHECKING:
    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/internal/agent/s3", tags=["agent-internal"])


@router.post("/{file_id}/uploaded", status_code=status.HTTP_200_OK, response_model=UploadedResponse)
async def report_uploaded(
    file_id: uuid.UUID,
    body: UploadedRequest,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UploadedResponse:
    """Record a successful upload: complete the multipart CONTROL-SIDE + ``UPLOADING -> UPLOADED``.

    The control plane (not the agent) completes the multipart upload (KSTAGE-01/DIST-01) using the
    agent-reported ``(part_number, etag)`` pairs, then flips ``cloud_job`` with a rowcount guard so a
    duplicate/late callback is an idempotent 200 no-op that does NOT re-complete the object
    (T-53-15). ``file_id`` is the PATH value only; ``agent`` comes from the token (AUTH-01).

    Phase 55 (D-01b, KROUTE-03): on the k8s target the upload-complete callback is also the
    post-staging seam -- it advances the FileRecord ``PUSHING -> PUSHED`` (a rowcount-guarded
    idempotent flip mirroring ``agent_push.report_pushed``, freeing a window slot) and enqueues
    ``submit_cloud_job`` through ``enqueue_router`` on the controller queue (NEVER a raw enqueue --
    KROUTE-04). A1 uses rsync and never reaches these S3 callbacks, so the ``cloud_target == "k8s"``
    guard is defensive: a non-k8s target preserves today's cloud_job-only behavior.
    """
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()

    # No staging row, already past UPLOADING (completed/failed), or no multipart to complete:
    # idempotent 200, never re-complete.
    if cloud_job is None or cloud_job.status != CloudJobStatus.UPLOADING.value or cloud_job.upload_id is None:
        logger.info("report_uploaded: idempotent no-op (cloud_job absent or not UPLOADING)", file_id=str(file_id), agent_id=agent.id)
        return UploadedResponse(file_id=file_id)

    # Complete the multipart upload control-side with the agent-reported parts (KSTAGE-01).
    await s3_staging.complete_multipart_upload(file_id, cloud_job.upload_id, [(p.part_number, p.etag) for p in body.parts])

    # Idempotent flip guarded on the CURRENT status so a concurrent duplicate that also passed the
    # pre-check above does not double-flip. An UPDATE returns a CursorResult at runtime (exposing
    # rowcount); the async stubs type it as the base Result, so cast to read the affected-row count.
    res = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CloudJob)
            .where(CloudJob.file_id == file_id, CloudJob.status == CloudJobStatus.UPLOADING.value)
            .values(status=CloudJobStatus.UPLOADED.value)
        ),
    )
    if res.rowcount == 0:
        await session.commit()
        logger.info("report_uploaded: idempotent no-op (lost the flip race)", file_id=str(file_id), agent_id=agent.id)
        return UploadedResponse(file_id=file_id)

    # Phase 55 (D-01b): k8s post-staging seam. Advance the FileRecord PUSHING -> PUSHED and enqueue
    # the routed submit_cloud_job. Defensive guard -- a1 uses rsync and never hits these callbacks,
    # so a non-k8s target keeps today's cloud_job-only flow.
    settings = cast("ControlSettings", get_settings())
    if settings.cloud_target == "k8s":
        # Rowcount-guarded idempotent flip (mirrors agent_push.report_pushed): a duplicate/late
        # callback whose file already advanced past PUSHING matches 0 rows -> NO re-enqueue (T-55-SEAM-05).
        flip = cast(
            "CursorResult[Any]",
            await session.execute(
                update(FileRecord).where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING).values(state=FileState.PUSHED)
            ),
        )
        if flip.rowcount == 0:
            await session.commit()
            logger.info("report_uploaded: idempotent no-op (file no longer PUSHING)", file_id=str(file_id), agent_id=agent.id)
            return UploadedResponse(file_id=file_id)
        # Route submit_cloud_job onto the CONTROLLER queue via the single Phase-30 seam (never a raw
        # controller_queue.enqueue / the default queue -- KROUTE-04, T-55-SEAM-03). Deterministic key
        # dedups a replayed submit (KSUBMIT-01). submit_cloud_job stays staging-free (rejected coupling).
        routed = await resolve_queue_for_task("submit_cloud_job", request.app.state, session)
        await routed.queue.enqueue("submit_cloud_job", key=submit_cloud_job_key(file_id), file_id=str(file_id))
        await session.commit()
        logger.info("report_uploaded: FileRecord -> PUSHED + submit_cloud_job routed", file_id=str(file_id), agent_id=agent.id)
        return UploadedResponse(file_id=file_id)

    await session.commit()
    logger.info("report_uploaded: multipart completed + cloud_job -> UPLOADED", file_id=str(file_id), agent_id=agent.id)
    return UploadedResponse(file_id=file_id)


@router.post("/{file_id}/failed", status_code=status.HTTP_200_OK, response_model=UploadFailedResponse)
async def report_upload_failed(
    file_id: uuid.UUID,
    body: UploadFailedRequest,
    request: Request,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UploadFailedResponse:
    """Record an upload failure: bounded re-drive, or terminal cleanup at the cap (KSTAGE-04).

    The ``s3_upload_attempt`` counter lives in the ``s3_upload:<file_id>`` ledger payload JSONB
    (migration-free, Pitfall 4). Read it (default 0) and increment:

    - ``attempt + 1 > push_max_attempts`` -> ``cloud_job`` FAILED + abort the multipart + delete the
      staged object + clear the ledger, in one transaction: the terminal cleanup that prevents an
      orphaned in-flight upload / leaked object (KSTAGE-04 / T-53-17).
    - otherwise -> re-drive the upload (``cloud_staging.redrive_upload``: abort the prior multipart +
      re-stage with a fresh upload) keeping ``cloud_job`` UPLOADING, and stamp the incremented
      attempt back on the ledger row (T-53-16). With no fileserver online this is a clean 200 hold
      (NoActiveAgentError caught), never a 500 (T-53-19).

    ``file_id`` is the PATH value only; ``agent`` from the token (AUTH-01). ``body.detail`` is a
    bounded optional diagnostic that carries no identity.
    """
    settings = cast("ControlSettings", get_settings())
    ledger_key = f"s3_upload:{file_id}"

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key))).scalar_one_or_none()
    current_attempt = 0
    if row is not None and isinstance(row.payload, dict):
        current_attempt = int(row.payload.get("s3_upload_attempt", 0) or 0)
    next_attempt = current_attempt + 1

    # Over the cap: terminal failure + cleanup (abort + delete) + ledger clear, one transaction.
    if next_attempt > settings.push_max_attempts:
        cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
        await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.FAILED.value))
        if cloud_job is not None and cloud_job.upload_id:
            await s3_staging.abort_multipart_upload(file_id, cloud_job.upload_id)
        await s3_staging.delete_staged_object(file_id)
        await clear_ledger_entry(session, ledger_key)
        await session.commit()
        logger.warning(
            "report_upload_failed: re-drive cap reached -> cloud_job FAILED + cleaned up",
            file_id=str(file_id),
            agent_id=agent.id,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
            detail=body.detail,
        )
        return UploadFailedResponse(file_id=file_id, cleared=True)

    # Under the cap: re-drive the upload, keeping the cloud_job UPLOADING. Load the FileRecord by the
    # PATH file_id (AUTH-01) so redrive_upload has the source path / size; an unknown file_id with a
    # re-drive request is malformed -> 404 (mirrors the presign-download load, 53-02).
    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown file_id")

    try:
        await cloud_staging.redrive_upload(session, file, request.app.state.task_router)
    except NoActiveAgentError:
        # No fileserver online: leave the cloud_job UPLOADING for a later re-drive; clean 200 hold.
        await session.commit()
        logger.warning("report_upload_failed held: no fileserver agent online", file_id=str(file_id), agent_id=agent.id, attempt=next_attempt)
        return UploadFailedResponse(file_id=file_id, cleared=False)

    # Stamp the incremented attempt back on the ledger payload. redrive_upload -> stage_file_to_s3
    # commits a FRESH payload (new presigned part_urls) to THIS same ledger row via its enqueue hook,
    # so the `row` snapshot read at the top of the handler is now stale. Re-fetch the row (READ
    # COMMITTED + populate_existing busts the identity map so we see redrive's committed payload) and
    # build `merged` on top of the FRESH payload -- otherwise the attempt stamp would clobber the
    # fresh part_urls with the expired ones, making recovery replay re-enqueue dead URLs (WR-02).
    refreshed = (
        await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key).execution_options(populate_existing=True))
    ).scalar_one_or_none()
    base_payload: dict[str, Any] = dict(refreshed.payload) if (refreshed is not None and isinstance(refreshed.payload, dict)) else {}
    merged: dict[str, Any] = {**base_payload, "s3_upload_attempt": next_attempt}
    await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == ledger_key).values(payload=merged))
    await session.commit()

    logger.info("report_upload_failed: re-driving upload (slot retained)", file_id=str(file_id), agent_id=agent.id, attempt=next_attempt)
    return UploadFailedResponse(file_id=file_id, cleared=False)
