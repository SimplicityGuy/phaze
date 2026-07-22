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
from dataclasses import dataclass, field
import math
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.schemas.agent_s3 import UploadFileS3Payload
from phaze.services import s3_staging
from phaze.services.enqueue_router import lane_for_task, select_active_agent
from phaze.tasks.s3_upload import S3_UPLOAD_SAQ_RETRIES, upload_file_saq_timeout_sec


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BucketConfig
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


# phaze-grzo: the session.info key under which a staging body PARKS its s3_upload enqueue until the
# caller has durably committed the cloud_job UPLOADING row. Enqueue-before-commit was a dual-write
# ordering hole: SAQ's PostgresQueue enqueues on its OWN psycopg pool and commits the job durably +
# immediately, independent of THIS asyncpg session, so a fast agent could dequeue s3_upload and POST
# /uploaded before the cloud_job UPLOADING row the callback reads was committed -- report_uploaded
# then sees no UPLOADING row and idempotently no-ops, stranding the file. The fix defers the enqueue
# past the caller's commit so the worker-visible side effect can never precede the row it reads.
_PENDING_ENQUEUE_KEY = "cloud_staging_pending_s3_enqueues"


@dataclass(frozen=True)
class _PendingS3Enqueue:
    """One deferred ``s3_upload`` enqueue: the resolved queue + the enqueue kwargs, flushed post-commit."""

    queue: Any
    enqueue_kwargs: dict[str, Any] = field(default_factory=dict)


def _park_s3_enqueue(session: AsyncSession, pending: _PendingS3Enqueue) -> None:
    """Record a deferred ``s3_upload`` enqueue on the session, to be flushed AFTER the caller commits."""
    session.info.setdefault(_PENDING_ENQUEUE_KEY, []).append(pending)


def drop_pending_s3_enqueues(session: AsyncSession) -> None:
    """Discard any parked ``s3_upload`` enqueues WITHOUT firing them (phaze-grzo).

    The caller MUST call this whenever the transaction that produced the parked enqueues is rolled
    back: firing an enqueue whose ``cloud_job`` upsert was rolled back is the ORPHANING half of the
    dual-write hole (a job runs against a row that never committed). Dropping the parked enqueues on
    rollback closes that variant.
    """
    session.info.pop(_PENDING_ENQUEUE_KEY, None)


async def flush_pending_s3_enqueues(session: AsyncSession) -> int:
    """Fire every ``s3_upload`` enqueue parked on ``session`` and return the count fired (phaze-grzo).

    MUST be called ONLY after the caller has committed the ``cloud_job`` UPLOADING row(s) the parked
    jobs depend on, so the worker-visible side effect can never precede its committed row. Best-effort
    per item: an enqueue failure leaves that file's row committed-but-UPLOADING (a stranded row the
    age-bounded ``_reap_stranded_staging`` reaper spills back to awaiting -- phaze-ul2v), and must not
    block the remaining enqueues. The list is popped up front so a partial flush never double-fires.
    """
    pending: list[_PendingS3Enqueue] = session.info.pop(_PENDING_ENQUEUE_KEY, [])
    fired = 0
    for item in pending:
        try:
            # Phase 36: the PostgresQueue broker pool is built open=False; connect() is idempotent.
            await item.queue.connect()
            job = await item.queue.enqueue("s3_upload", **item.enqueue_kwargs)
            if job is None:
                # phaze-oj7x: SAQ deduped the deterministic key against a still-incomplete
                # ``s3_upload:<file_id>`` job (its ON CONFLICT only overwrites an aborted/complete/failed
                # row). This is the re-drive-during-active-job window: the flush did NOT land a fresh job.
                # It is benign rather than a silent poison -- the prior job carries retries=0, so it
                # settles terminal (releasing the key) and the control re-drive / stranded-staging reaper
                # re-enqueues cleanly on the next pass. Surface it loudly instead of claiming a re-drive
                # that never ran.
                logger.warning(
                    "flush_pending_s3_enqueues: s3_upload enqueue deduped against a still-incomplete job "
                    "(fresh job NOT landed; prior job settles terminal via retries=0, re-drive lands on next pass)",
                    key=item.enqueue_kwargs.get("key"),
                )
            else:
                fired += 1
        except Exception:
            # A parked enqueue that fails leaves the committed UPLOADING row for the staging reaper to
            # spill back to awaiting; never let one failed enqueue abort the rest of the flush.
            logger.warning(
                "flush_pending_s3_enqueues: parked s3_upload enqueue failed -> row left for the staging reaper",
                key=item.enqueue_kwargs.get("key"),
                exc_info=True,
            )
    return fired


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

    phaze-grzo: the core PARKS its ``s3_upload`` enqueue rather than firing it inline; this wrapper
    commits the ``cloud_job`` UPLOADING row FIRST and only then flushes the parked enqueue, so the
    worker-visible job (and its ``report_uploaded`` callback) can never precede the committed row it
    reads. On a commit failure the parked enqueue is dropped (never fired against a rolled-back row).
    """
    try:
        await _stage_file_to_s3(session, file, task_router, bucket)
        await session.commit()
    except BaseException:
        drop_pending_s3_enqueues(session)
        raise
    await flush_pending_s3_enqueues(session)


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
    4. PARK exactly one ``s3_upload`` job on the session (phaze-grzo) carrying the presigned part
       URLs, the part size, and the file_id, with the deterministic ``s3_upload:<file_id>`` key and
       the part-count-scaled job-net timeout (WR-03). The caller fires it via
       :func:`flush_pending_s3_enqueues` AFTER committing the ``cloud_job`` UPLOADING row.

    phaze-grzo: step 4 no longer fires the enqueue inline. SAQ's ``PostgresQueue`` enqueues on its
    OWN psycopg pool and commits the job durably + IMMEDIATELY, independent of this asyncpg session's
    commit boundary. Firing it inside the staging body (before the caller's commit) let a fast agent
    dequeue ``s3_upload`` and POST ``/uploaded`` before the ``cloud_job`` UPLOADING row was committed;
    ``report_uploaded`` then saw no UPLOADING row and idempotently no-op'd, STRANDING the file (the row
    later commits UPLOADING with the multipart never completed -- nothing recovers it but the age
    reaper, and it permanently consumes an ``in_flight_count`` cap slot). Parking the enqueue and
    firing it post-commit makes the worker-visible job strictly follow its committed row. On a drain
    rollback the caller drops the parked enqueues (:func:`drop_pending_s3_enqueues`) so a rolled-back
    upsert never leaves an orphaned job. This supersedes the old phaze-uciu.3 ``begin_nested()``
    SAVEPOINT: the enqueue is no longer in the transaction, so there is no enqueue-failure to isolate
    from the upsert -- the upsert runs directly in the caller's transaction.

    phaze-bbwx: everything from ``presign_upload_parts`` through the parked-enqueue registration
    runs under a try/except that best-effort aborts the fresh multipart upload before re-raising.
    Without this, a failure in that window discards the only durable record of ``upload_id`` (the
    upsert either never ran or its transaction is destined to roll back), so no later cleanup path
    (``redrive_upload``'s abort, ``report_upload_failed``'s terminal abort) can ever find it to
    abort. ``ensure_bucket_lifecycle_ttl``'s ``AbortIncompleteMultipartUpload`` rule
    (``s3_staging.py``, phaze-sqpv) is the eventual backstop if this best-effort abort itself fails.
    """
    cfg = cast("ControlSettings", get_settings())

    # Gate on an online fileserver agent BEFORE mutating anything: with none available this is a
    # clean hold (NoActiveAgentError propagates) -- no multipart, no cloud_job, no parked enqueue.
    agent = await select_active_agent(session, kind="fileserver")

    upload_id = await s3_staging.create_multipart_upload(file.id, bucket)
    try:
        part_count = max(1, math.ceil(file.file_size / cfg.s3_multipart_part_size_bytes))
        part_urls = await s3_staging.presign_upload_parts(file.id, upload_id, part_count, bucket)

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
                # phaze-2hv9: bump the lane-entry / staleness clock on EVERY re-stage. CloudJob.updated_at is a
                # client-side ``onupdate=func.now()`` (TimestampMixin), which SQLAlchemy does NOT inject into an
                # ON CONFLICT DO UPDATE SET clause, and there is no DB trigger -- so without this the conflict
                # (re-stage / re-drive) path would leave updated_at frozen at the FIRST dispatch. KueueBackend's
                # ``_reap_stranded_staging`` ages a row off ``now - updated_at``: a frozen clock lets a live
                # re-driven upload inherit the whole prior attempt's elapsed time and be reaped mid-transfer.
                # Stamp it explicitly here so any re-stage resets that clock (mirrors agent_bootstrap.py's idiom).
                "updated_at": func.now(),
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
        # phaze-grzo: PARK the enqueue -- do NOT fire it here. The caller flushes it AFTER committing the
        # cloud_job UPLOADING row so the job (and its report_uploaded callback) never precedes that row.
        _park_s3_enqueue(
            session,
            _PendingS3Enqueue(
                queue=queue,
                enqueue_kwargs={
                    "key": f"s3_upload:{file.id}",
                    # phaze-g37f: scale the SAQ job-net timeout with the part count so a multi-GB upload is
                    # not deterministically cancelled by a fixed single-part cap. Each part carries its own
                    # asyncio guard on the agent, so the net sits strictly above the SUM of those budgets.
                    "timeout": upload_file_saq_timeout_sec(part_count),
                    # phaze-oj7x: pin retries EXPLICITLY to 0 (S3_UPLOAD_SAQ_RETRIES). Control (re-drive + reaper)
                    # is the sole re-drive vehicle; an unset retries would be clobbered to worker_max_retries by the
                    # before_enqueue hook, re-arming SAQ to replay the ORIGINAL payload against a multipart this very
                    # re-drive already aborted (guaranteed NoSuchUpload). See S3_UPLOAD_SAQ_RETRIES for the full note.
                    "retries": S3_UPLOAD_SAQ_RETRIES,
                    **payload.model_dump(mode="json"),
                },
            ),
        )
    except Exception:
        # Best-effort compensation (phaze-bbwx): upload_id is about to become unrecoverable (never
        # persisted, or the caller's transaction will roll back the row that would have persisted it),
        # so this is the only chance to abort it. Idempotent + swallows already-gone codes on its own
        # (s3_staging.abort_multipart_upload); ANY exception the abort itself raises -- a wrapped
        # S3StagingError, or a raw network/DNS error the client context manager can surface before
        # the SDK call even reaches botocore's ClientError wrapping -- is logged, never raised, so a
        # failed compensation can never mask the ORIGINAL failure. The lifecycle backstop
        # (phaze-sqpv) is the last resort if this abort itself cannot reach S3.
        try:
            await s3_staging.abort_multipart_upload(file.id, upload_id, bucket)
        except Exception:
            logger.warning(
                "stage_file_to_s3: best-effort abort of orphaned multipart upload failed",
                file_id=str(file.id),
                upload_id=upload_id,
                exc_info=True,
            )
        raise


    logger.info(
        "stage_file_to_s3: cloud_job staged + s3_upload enqueue parked (fires post-commit, phaze-grzo)",
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
    # phaze-j2tm: call the NO-COMMIT core, NOT the committing wrapper. The sole caller
    # (POST /agents/s3/{file_id}/failed) holds a transaction-scoped ``pg_advisory_xact_lock`` to
    # serialize the s3_upload_attempt read->+1->write-back (D-11/T-83-02). ``stage_file_to_s3``'s inner
    # ``session.commit()`` would auto-RELEASE that lock mid-handler, so a concurrent /failed could
    # acquire it and re-read the (hook-rewritten, counter-less) ledger payload as attempt 0 before the
    # handler stamps the increment -- a lost update that defeats the bounded re-drive cap. Leaving the
    # commit to the handler keeps the lock held through the attempt stamp, serializing the RMW.
    await _stage_file_to_s3(session, file, task_router, bucket)
