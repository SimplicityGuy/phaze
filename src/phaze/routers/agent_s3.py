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
  dedicated ``scheduling_ledger.redrive_attempt`` column keyed by ``s3_upload:<file_id>`` (phaze-y0j0:
  OUTSIDE the hook-rewritten ``payload`` JSONB so the bounded budget survives a crash mid-re-drive). Under
  ``push_max_attempts`` control re-drives the upload (``cloud_staging.redrive_upload``: abort the
  prior multipart + re-stage) keeping ``cloud_job`` UPLOADING and stamps the incremented attempt
  back (T-53-16). At/over the cap control spills ``cloud_job`` back to ``awaiting`` + clears the ledger
  (committed FIRST), THEN aborts the multipart + deletes the staged object POST-COMMIT (phaze-1v37:
  best-effort cleanup that holds no lock across the S3 round-trip; lifecycle TTL backstops a miss) --
  the terminal cleanup that prevents orphaned in-flight uploads / leaked objects (KSTAGE-04 / T-53-17).
  With no fileserver online the re-drive is a clean 200 hold (NoActiveAgentError caught), never a 500
  (T-53-19).

phaze-1v37: every control-side S3 SDK call runs OUTSIDE the request's DB transaction -- ``/uploaded``
releases the (lock-free) read before ``complete_multipart_upload`` and re-opens a transaction for the
idempotent CAS; ``/failed`` commits the spill CAS + ledger clear before the abort/delete cleanup -- so
a wedged S3 endpoint never pins a pooled connection idle-in-transaction (draining the small PgBouncer
pool and 500ing the control plane). The S3 client is additionally bounded with explicit connect/read
timeouts (``s3_client_timeout_sec``). The under-cap ``redrive_upload`` S3 setup still runs under the
attempt-counter advisory lock (its ledger RMW is coupled to that lock); the bounded client timeouts cap
that residual pin.

AUTH-01 discipline: ``file_id`` always travels on the URL PATH; the agent identity comes from the
token dependency. The request bodies carry NO identity (``extra="forbid"`` on the schemas).
"""

from typing import TYPE_CHECKING, Annotated, Any, cast
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from phaze.config import get_settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_s3 import UploadedRequest, UploadedResponse, UploadFailedRequest, UploadFailedResponse
from phaze.services import cloud_staging, s3_staging
from phaze.services.backends import hold_awaiting_cloud, resolved_non_local_kind
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

    Phase 55 (D-01b, KROUTE-03): on the kueue target the upload-complete callback is also the
    post-staging seam -- it enqueues ``submit_cloud_job`` through ``enqueue_router`` on the
    controller queue (NEVER a raw enqueue -- KROUTE-04). Phase 90 (D-09) removed the companion
    FileRecord ``PUSHING -> PUSHED`` CAS flip this seam used to perform; idempotency is now carried
    solely by the outer ``cloud_job`` CAS (``UPLOADING -> UPLOADED`` above) plus the deterministic
    ``submit_cloud_job`` key. A1 uses rsync and never reaches these S3 callbacks, so the
    resolved-kind ``== "kueue"`` guard is defensive: a non-kueue target preserves today's
    cloud_job-only behavior.
    """
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()

    # No staging row, already past UPLOADING (completed/failed), or no multipart to complete:
    # idempotent 200, never re-complete.
    if cloud_job is None or cloud_job.status != CloudJobStatus.UPLOADING.value or cloud_job.upload_id is None:
        logger.info("report_uploaded: idempotent no-op (cloud_job absent or not UPLOADING)", file_id=str(file_id), agent_id=agent.id)
        return UploadedResponse(file_id=file_id)

    settings = cast("ControlSettings", get_settings())

    # phaze-eo5x: an EMPTY parts list is a degenerate/zero-byte upload (the agent's _transfer_parts
    # returns [] for a 0-byte source: the first read yields b'' and breaks before any PUT). S3 multipart
    # REQUIRES >=1 part -- CompleteMultipartUpload rejects an empty list with MalformedXML, which
    # s3_staging re-raises as S3StagingError (it swallows only NoSuchUpload/404). report_uploaded does not
    # catch it, so it escaped as an unhandled 500 that the SAQ retry reproduced forever, permanently
    # stranding cloud_job UPLOADING and leaking the in-flight cap slot + the multipart. There is NO valid
    # completion for zero parts, so drive a clean terminal resolution that FREES the job instead: spill the
    # cloud_job back to 'awaiting' with its cloud budget SPENT (so select_backend routes the file to LOCAL,
    # where a 0-byte file terminally fails analysis -- the uniform failure funnel), abort the orphaned
    # multipart + delete any staged object, and clear the ledger -- the SAME terminal cleanup the over-cap
    # /failed branch runs (KSTAGE-04 / T-53-17). Return a definitive 200 so the agent stops retrying.
    if not body.parts:
        bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket)
        # NULL-GUARD: hold_awaiting_cloud's spill CAS dereferences file.id (mirrors the /failed over-cap
        # branch); an absent file (unreachable -- cloud_job.file_id FKs files.id) takes the full no-op.
        file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
        upload_id = cloud_job.upload_id
        cleared = file is not None and await hold_awaiting_cloud(
            session,
            file,
            attempts=settings.cloud_submit_max_attempts,
            expect_status=(CloudJobStatus.UPLOADING.value, CloudJobStatus.UPLOADED.value),
            clear_cloud_phase=True,
        )
        if cleared:
            await clear_ledger_entry(session, f"s3_upload:{file_id}")
        await session.commit()
        # phaze-1v37: run the S3 abort + delete AFTER the commit (best-effort, lifecycle TTL backstop),
        # so the spill CAS + ledger clear are durable and no transaction/row lock is held across the S3
        # round-trip (mirrors the /failed over-cap branch and _delete_staged_object_if_cloud).
        if cleared and bucket is not None:
            if upload_id:
                await s3_staging.abort_multipart_upload(file_id, upload_id, bucket)
            await s3_staging.delete_staged_object(file_id, bucket)
        logger.warning(
            "report_uploaded: empty parts list (zero-byte/degenerate upload) -> cloud_job spilled to awaiting (routes local) + cleaned up",
            file_id=str(file_id),
            agent_id=agent.id,
            cleared=cleared,
        )
        return UploadedResponse(file_id=file_id)

    # Complete the multipart upload control-side with the agent-reported parts (KSTAGE-01), on the
    # RECORDED staging bucket (MKUE-02 -- a kueue UPLOADING row always carries the staging_bucket
    # KueueBackend.dispatch stamped; resolve it, never re-derive).
    bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket)
    if bucket is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="staged upload has no resolvable staging bucket recorded",
        )
    # phaze-1v37: capture the ORM values needed for the S3 call into locals, then RELEASE the read
    # transaction BEFORE the multipart-complete round-trip so the pooled connection is not pinned
    # idle-in-transaction across S3 network I/O (a wedged/blackholed endpoint would otherwise hold a
    # PgBouncer SESSION-mode upstream connection for the full botocore window, and a handful of
    # concurrent callbacks would drain the small pool and 500 the whole control plane). The SELECT above
    # took NO row lock (READ COMMITTED) and wrote nothing, so committing here just returns the connection
    # to the pool. The idempotent UPLOADING->UPLOADED CAS below re-opens a fresh transaction after the
    # S3 call, and its rowcount guard still makes a concurrent duplicate a clean no-op.
    upload_id = cloud_job.upload_id
    parts = [(p.part_number, p.etag) for p in body.parts]
    await session.commit()

    await s3_staging.complete_multipart_upload(file_id, upload_id, parts, bucket)

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

    # Phase 55 (D-01b): kueue post-staging seam. Advance the FileRecord PUSHING -> PUSHED and enqueue
    # the routed submit_cloud_job. Defensive guard -- a1 uses rsync and never hits these callbacks,
    # so a non-kueue target keeps today's cloud_job-only flow. (``settings`` resolved above.)
    # Phase 68 (D-09): registry-derived kind via the Backend registry helper (was the retired ≤1-non-local accessor).
    if resolved_non_local_kind(settings) == "kueue":
        # Phase 90 (D-09): the FileRecord PUSHING -> PUSHED CAS flip was removed here (read + write
        # deleted atomically in PR-B). Idempotency is preserved by the OUTER cloud_job CAS above
        # (UPLOADING -> UPLOADED, rowcount==0 early-returns before reaching this block) PLUS the
        # deterministic submit_cloud_job key -- a duplicate/late callback is already a no-op at the
        # cloud_job sidecar (the sole derived authority PR-A reads), so no state guard is load-bearing.
        # Route submit_cloud_job onto the CONTROLLER queue via the single Phase-30 seam (never a raw
        # controller_queue.enqueue / the default queue -- KROUTE-04, T-55-SEAM-03). Deterministic key
        # dedups a replayed submit (KSUBMIT-01). submit_cloud_job stays staging-free (rejected coupling).
        routed = await resolve_queue_for_task("submit_cloud_job", request.app.state, session)
        await routed.queue.enqueue("submit_cloud_job", key=submit_cloud_job_key(file_id), file_id=str(file_id))
        await session.commit()
        logger.info("report_uploaded: submit_cloud_job routed", file_id=str(file_id), agent_id=agent.id)
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

    The ``s3_upload_attempt`` counter lives in the dedicated ``scheduling_ledger.redrive_attempt``
    column keyed by ``s3_upload:<file_id>`` (phaze-y0j0: OUTSIDE the hook-rewritten ``payload`` JSONB so
    the bounded budget survives a crash in the re-drive->stamp window). Read it (default 0) and increment:

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

    # D-11 (T-83-02): serialize the s3_upload_attempt read->+1->write-back so two concurrent /failed can't
    # both read the same counter and lose an increment (letting a file exceed its bounded upload budget).
    # A transaction-scoped ADVISORY lock keyed by the ledger key -- NOT a `.with_for_update()` row lock:
    # the under-cap path re-enqueues the s3_upload job via redrive_upload -> stage_file_to_s3 while THIS
    # transaction is still open, and s3_upload is a registered before_enqueue key-builder, so
    # apply_deterministic_key upserts THIS SAME ledger row from its OWN session (ON CONFLICT DO UPDATE). A
    # row lock we hold would self-deadlock that nested write (no statement_timeout to break it, no Postgres
    # deadlock cycle to detect); the advisory lock lives in a different lock space, so the hook's upsert
    # never blocks on it and a second concurrent /failed waits on the advisory lock until we commit -- the
    # RMW is serialized and each increment is applied exactly once (mirrors agent_push.py:240).
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(ledger_key))))

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key))).scalar_one_or_none()
    current_attempt = 0
    if row is not None and row.redrive_attempt is not None:
        current_attempt = int(row.redrive_attempt)
    next_attempt = current_attempt + 1

    # Over the cap: CAS-guarded terminal spill (D-09/D-10/D-03) + cleanup + ledger clear, one transaction.
    if next_attempt > settings.push_max_attempts:
        cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
        # D-01/D-02: route the over-cap spill re-stamp through the SINGLE awaiting writer
        # (services.backends.hold_awaiting_cloud) instead of an inline CAS. Its spill branch preserves the
        # exact shipped guard: D-09 anchors on cloud_job.status IN ('uploading','uploaded') (the sidecar is
        # the single CAS domain, NOT FileRecord.state; SC#1); D-03 re-stamps the row to status='awaiting'
        # (was FAILED) with attempts SPENT (>= cloud_submit_max_attempts) so select_backend routes the
        # spilled file to LOCAL; clear_cloud_phase=True nulls cloud_phase (WR-01, off the "Running" tile,
        # D-12). It returns False (a full no-op) when a late/duplicate /failed matches an already-advanced
        # row (running/succeeded) -> the agent_s3.py:195 clobber stays closed (SC#2 / T-83-01).
        #
        # NULL-GUARD: the helper's CAS dereferences file.id, so load the FileRecord first. An absent file
        # (unreachable in practice -- cloud_job.file_id FKs files.id, so a cloud_job cannot outlive its file)
        # takes the FULL no-op below (cleared=False), identical to a CAS miss; passing None would raise
        # AttributeError where the old disconnected update(FileRecord) silently matched 0 rows. No 404 here:
        # the over-cap spill is an agent callback and a 404 would change the response contract.
        file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
        cleared = file is not None and await hold_awaiting_cloud(
            session,
            file,
            attempts=settings.cloud_submit_max_attempts,
            expect_status=(CloudJobStatus.UPLOADING.value, CloudJobStatus.UPLOADED.value),
            clear_cloud_phase=True,
        )
        if not cleared:
            # D-10: FULL no-op -- NO FileRecord write, NO multipart abort, NO delete_staged_object (a live
            # Kueue job may be mid-download on the object; KSTAGE-04 still holds via the analyze-terminal
            # seams that own _delete_staged_object_if_cloud), NO ledger clear. Commit and return
            # cleared=False (mirrors report_push_mismatch's over-cap no-op exactly).
            await session.commit()
            logger.info(
                "report_upload_failed: idempotent no-op (cloud_job no longer uploading/uploaded, over-cap spill skipped)",
                file_id=str(file_id),
                agent_id=agent.id,
            )
            return UploadFailedResponse(file_id=file_id, cleared=False)
        # cleared (helper CAS hit): gate S3 cleanup + ledger clear behind the CAS.
        # Phase 90 (D-09): the former AWAITING_CLOUD FileRecord.state dual-write was removed; the
        # cloud_job sidecar re-stamped to 'awaiting' by hold_awaiting_cloud is the sole derived authority.
        # MKUE-02: act on the RECORDED staging bucket; a bucketless row (no S3 object) skips the S3 ops cleanly.
        # phaze-1v37: capture the bucket + upload_id into locals and COMMIT the spill CAS + ledger clear
        # FIRST, then run the S3 abort + delete AFTER the commit. Pre-1v37 they ran while the transaction
        # (holding the pg_advisory_xact_lock taken at the top AND the cloud_job row lock the spill CAS took)
        # was still open, pinning the pooled connection across the S3 round-trip and blocking the staging
        # reaper + sibling /failed callbacks for the full botocore window. Post-commit the cleanup is
        # best-effort over durable state (the lifecycle TTL backstops a miss, KSTAGE-04 / T-53-17) and holds
        # no lock -- mirroring _delete_staged_object_if_cloud's record-first discipline (phaze-uoiw).
        bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket) if cloud_job is not None else None
        upload_id = cloud_job.upload_id if cloud_job is not None else None
        await clear_ledger_entry(session, ledger_key)
        await session.commit()
        if bucket is not None:
            if upload_id:
                await s3_staging.abort_multipart_upload(file_id, upload_id, bucket)
            await s3_staging.delete_staged_object(file_id, bucket)
        logger.warning(
            "report_upload_failed: re-drive cap reached -> cloud_job re-stamped to awaiting + cleaned up + spill to AWAITING_CLOUD (routes to local)",
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

    # Stamp the incremented attempt into the DEDICATED `redrive_attempt` column. redrive_upload ->
    # stage_file_to_s3 commits a FRESH payload (new presigned part_urls) to THIS same ledger row via
    # its enqueue hook, from its own session, BEFORE control returns here. Because the counter now
    # lives in `redrive_attempt` (a column the hook's ON CONFLICT DO UPDATE never touches) and NOT in
    # `payload`, this stamp: (1) cannot clobber the hook's fresh part_urls -- the old WR-02 re-fetch
    # dance is unnecessary; and (2) if a crash lands between the hook's commit and this commit, the
    # column keeps its prior value (un-incremented at `current_attempt`) instead of being reset to 0,
    # so the bounded upload budget survives the crash window (phaze-y0j0).
    await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == ledger_key).values(redrive_attempt=next_attempt))
    await session.commit()
    # phaze-grzo: redrive_upload PARKS its fresh s3_upload enqueue on the session; fire it ONLY now
    # that the re-driven cloud_job (still UPLOADING) and the attempt stamp are durably committed, so the
    # re-driven job (and its report_uploaded callback) can never precede the committed row it reads. A
    # commit failure above raises before this line, so the parked enqueue is never fired against a
    # rolled-back row (the request-scoped session is discarded, dropping the parked entry).
    await cloud_staging.flush_pending_s3_enqueues(session)

    logger.info("report_upload_failed: re-driving upload (slot retained)", file_id=str(file_id), agent_id=agent.id, attempt=next_attempt)
    return UploadFailedResponse(file_id=file_id, cleared=False)
