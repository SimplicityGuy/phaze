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
from typing import TYPE_CHECKING, Any, Literal, cast
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
    from sqlalchemy.dialects.postgresql import Insert
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BucketConfig
    from phaze.models.file import FileRecord
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)


class NoCloudJobToRedriveError(s3_staging.S3StagingError):
    """A re-drive was asked for a file that has NO ``cloud_job`` row at all (phaze-k95r7).

    NOT a bucket problem, NOT an expiry problem, and NOT a transient one -- there is simply no staging
    attempt on record to re-drive. On the live deployment this meant the work had FINISHED and the
    ``cloud_job`` row had since been cleaned up, leaving an orphaned ``s3_upload`` ledger row behind.

    It is split out because the merged message it used to share
    (*"could not resolve a staging bucket"*, which the caller then logged as *"its payload is
    time-limited and cannot be regenerated right now"*) was WRONG for this case in every particular,
    and cost an investigation on 2026-08-08: nothing was time-limited, nothing was mis-bucketed, and
    the row was never a recovery candidate in the first place. Subclasses
    :class:`~phaze.services.s3_staging.S3StagingError` so existing handlers that treat a staging
    failure as "cannot regenerate right now" keep working unchanged; callers that want to distinguish
    the two catch this FIRST.
    """


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
    """One deferred ``s3_upload`` enqueue: the resolved queue + the enqueue kwargs, flushed post-commit.

    phaze-cws5: ALSO carries the ``(file_id, upload_id, bucket)`` triple identifying the real S3
    multipart upload this enqueue depends on. ``_stage_file_to_s3`` creates that multipart as a
    non-transactional external side effect BEFORE parking the enqueue; if the caller's transaction
    rolls back for ANY reason after that point -- not just an exception inside the core itself (the
    phaze-bbwx compensation already covers that), but also a poisoned transaction from a LATER
    candidate in the same drain tick, or the caller's own post-loop commit failing -- the cloud_job
    row that would have persisted ``upload_id`` never lands, and nothing else can ever find it to
    abort. Carrying the triple here lets :func:`drop_pending_s3_enqueues` close that gap.
    """

    queue: Any
    enqueue_kwargs: dict[str, Any] = field(default_factory=dict)
    file_id: uuid.UUID | None = None
    upload_id: str | None = None
    bucket: BucketConfig | None = None


def _park_s3_enqueue(session: AsyncSession, pending: _PendingS3Enqueue) -> None:
    """Record a deferred ``s3_upload`` enqueue on the session, to be flushed AFTER the caller commits."""
    session.info.setdefault(_PENDING_ENQUEUE_KEY, []).append(pending)


async def _best_effort_abort_orphaned_multipart(
    file_id: uuid.UUID, upload_id: str, bucket: BucketConfig, *, orphaned_by: Literal["staging_failure", "caller_rollback"]
) -> None:
    """Abort a multipart upload nothing can reach any more, swallowing every failure (phaze-bbwx / phaze-cws5).

    Both compensation paths in this module reach here at the one moment ``upload_id`` is about to
    become unrecoverable, and ``orphaned_by`` says WHICH:

    * ``"staging_failure"`` -- the staging body's own ``except`` (phaze-bbwx): the upsert either never
      ran or its transaction is destined to roll back, so this attempt failed on its own.
    * ``"caller_rollback"`` -- :func:`drop_pending_s3_enqueues`' per-item sweep (phaze-cws5): THIS
      staging attempt succeeded and the CALLER's transaction rolled back underneath it, which is a
      materially different operational event and usually points at a different candidate in the same
      drain tick.

    Past that moment no later cleanup path (``redrive_upload``'s abort, ``report_upload_failed``'s
    terminal abort, the staging reaper) can ever find the upload again, and it sits as an orphaned
    incomplete multipart on the bucket until the ``s3_lifecycle_ttl_days`` backstop
    (``ensure_bucket_lifecycle_ttl``, phaze-sqpv) expires it.

    ``orphaned_by`` is a LOGGED FACT, not a caller label, and it is load-bearing rather than
    decorative: a traceback records only the frames from the catching frame DOWNWARD, so once the
    ``try``/``except`` lives here instead of in each caller, ``exc_info`` renders identically for both
    paths (verified in this environment: both yield frames ``[_best_effort_abort_orphaned_multipart,
    abort_multipart_upload]``). Without this field an operator reading the log could no longer tell a
    failed staging attempt from a rolled-back drain tick. Do not delete it, and do not replace it with
    the calling function's name -- the cause outlives any rename of the caller.

    NEVER RAISES, and that is the whole contract. ``s3_staging.abort_multipart_upload`` is idempotent
    and swallows already-gone codes on its own, but ANY exception it can still surface -- a wrapped
    ``S3StagingError``, or a raw network/DNS error the client context manager raises before the SDK
    call even reaches botocore's ClientError wrapping -- is logged rather than propagated: a failed
    compensation must never mask the ORIGINAL failure, and one bad abort must never block the rest of
    a sweep. Guarded by ``test_stage_file_to_s3_logs_but_does_not_raise_when_abort_itself_fails`` and
    ``test_drop_pending_abort_failure_does_not_raise``.

    Touches NO database session: it is pure S3 I/O, so it can be called from either compensation path
    without changing where any transaction opens or closes relative to an S3 call.
    """
    try:
        await s3_staging.abort_multipart_upload(file_id, upload_id, bucket)
    except Exception:
        logger.warning(
            "cloud_staging: best-effort abort of an orphaned multipart upload failed "
            "(the s3_lifecycle_ttl_days backstop, if configured, is the last resort)",
            orphaned_by=orphaned_by,
            file_id=str(file_id),
            upload_id=upload_id,
            exc_info=True,
        )


async def drop_pending_s3_enqueues(session: AsyncSession) -> None:
    """Discard any parked ``s3_upload`` enqueues WITHOUT firing them, best-effort aborting their multiparts.

    The caller MUST call this whenever the transaction that produced the parked enqueues is rolled
    back: firing an enqueue whose ``cloud_job`` upsert was rolled back is the ORPHANING half of the
    dual-write hole (a job runs against a row that never committed). Dropping the parked enqueues on
    rollback closes that variant.

    phaze-cws5: the freshly-created multipart upload each dropped item names is now ALSO the caller's
    only remaining chance to abort it. Once the rollback completes, ``upload_id`` was never persisted
    anywhere (the row that would have carried it just rolled back), so no later cleanup path
    (``redrive_upload``'s abort, ``report_upload_failed``'s terminal abort, the staging reaper) can
    ever find it -- it would otherwise sit as an orphaned incomplete multipart on the bucket until the
    ``s3_lifecycle_ttl_days`` backstop (``ensure_bucket_lifecycle_ttl``) expires it. Best-effort PER
    ITEM (mirrors :func:`flush_pending_s3_enqueues`'s per-item discipline): an abort failure here must
    never mask the original rollback cause, and one bad abort must not block the rest.
    """
    pending: list[_PendingS3Enqueue] = session.info.pop(_PENDING_ENQUEUE_KEY, [])
    for item in pending:
        if item.file_id is None or item.upload_id is None or item.bucket is None:
            continue
        await _best_effort_abort_orphaned_multipart(item.file_id, item.upload_id, item.bucket, orphaned_by="caller_rollback")


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


@dataclass(frozen=True)
class _PresignedParts:
    """The presigned PUT URLs for one multipart upload, plus the part geometry the agent must slice to.

    ``part_size_bytes`` is the EFFECTIVE size the URLs were signed against, never the raw configured
    floor: handing the agent a different value than the presign used would make it cut bytes at the
    wrong boundaries and silently corrupt the assembled object.
    """

    part_size_bytes: int
    part_count: int
    urls: list[str]


async def _presign_multipart_parts(cfg: ControlSettings, file: FileRecord, upload_id: str, bucket: BucketConfig) -> _PresignedParts:
    """Presign every PUT URL for ``upload_id``, sized under S3's 10,000-part ceiling (phaze-wz1q, phaze-pq1fe).

    Derives an EFFECTIVE part size that can never push ``part_count`` past S3's 10,000-part multipart
    ceiling, regardless of how large -- or how badly misreported -- ``file.file_size`` is: the
    configured ``s3_multipart_part_size_bytes`` is a FLOOR, not the final word (phaze-wz1q). The same
    effective size is what the caller records on ``UploadFileS3Payload.part_size_bytes``, which is why
    it is returned alongside the URLs rather than re-read from config downstream.

    phaze-pq1fe: every part is signed with the SAME part-count-scaled TTL
    (``max(s3_presign_put_ttl_sec, upload_file_saq_timeout_sec(part_count))``) instead of the flat 1h
    default. The agent PUTs parts strictly SEQUENTIALLY (``tasks/s3_upload._transfer_parts``) under a
    per-part budget that already drives the SAQ job-net timeout, so reusing that same value here keeps
    the signature valid when the sequential transfer finally reaches a later part. It never drops
    BELOW the configured floor, so a short transfer keeps the operator's shorter default.

    Pure S3 I/O plus arithmetic -- it takes no session and opens no transaction, so it can sit
    anywhere in the staging body without moving a database scope relative to an S3 call.
    """
    part_size = max(cfg.s3_multipart_part_size_bytes, math.ceil(file.file_size / s3_staging.S3_MAX_PART_COUNT))
    part_count = max(1, math.ceil(file.file_size / part_size))
    presign_ttl_sec = max(cfg.s3_presign_put_ttl_sec, upload_file_saq_timeout_sec(part_count))
    urls = await s3_staging.presign_upload_parts(file.id, upload_id, part_count, bucket, expires_in_sec=presign_ttl_sec)
    return _PresignedParts(part_size_bytes=part_size, part_count=part_count, urls=urls)


def _uploading_cloud_job_upsert(file: FileRecord, upload_id: str, bucket: BucketConfig) -> Insert:
    """Build the idempotent ``cloud_job`` UPLOADING upsert for ``file`` (ON CONFLICT on the unique file_id FK).

    A re-stage refreshes the key/status/upload_id/staging_bucket in place instead of erroring on the
    duplicate (mirrors the ``scheduling_ledger`` upsert idiom).

    Builds the statement and NOTHING else: it takes no session, issues no I/O, and the caller keeps
    the ``session.execute`` at its own call site so the one piece of database work in the staging body
    stays visible exactly where it always sat between the S3 calls around it.
    """
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
    return stmt.on_conflict_do_update(
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
        await drop_pending_s3_enqueues(session)
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
    2. Refuse a ``file.file_size`` past S3's max object size, then initiate the multipart upload and
       presign its PUT URLs (:func:`_presign_multipart_parts`, which owns the phaze-wz1q part-count
       ceiling and the phaze-pq1fe TTL scaling). ``file.file_size`` is unvalidated agent wire input
       (``schemas/agent_files.py`` declines a storage-domain cap on purpose), and the size check is
       held HERE rather than inside the presign because it must fail BEFORE the multipart is created:
       an upload that can never complete should not exist on the bucket at all (phaze-wz1q).
    3. Upsert the ``cloud_job`` row (``UPLOADING`` + file_id-scoped key + multipart ``upload_id``)
       ON CONFLICT (file_id) so a re-stage is idempotent against the unique FK (no duplicate row).
       The statement is built by :func:`_uploading_cloud_job_upsert`; the ``session.execute`` stays in
       this body so the sole database touch remains visible among the S3 calls surrounding it.
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

    phaze-bbwx: everything from the presign through the parked-enqueue registration runs under a
    try/except that best-effort aborts the fresh multipart upload before re-raising
    (:func:`_best_effort_abort_orphaned_multipart`, shared with :func:`drop_pending_s3_enqueues`).
    Without this, a failure in that window discards the only durable record of ``upload_id`` (the
    upsert either never ran or its transaction is destined to roll back), so no later cleanup path
    (``redrive_upload``'s abort, ``report_upload_failed``'s terminal abort) can ever find it to
    abort. ``ensure_bucket_lifecycle_ttl``'s ``AbortIncompleteMultipartUpload`` rule
    (``s3_staging.py``, phaze-sqpv) is the eventual backstop if this best-effort abort itself fails.

    TRANSACTION SCOPES (the standing decision: keep database connections out of S3 I/O). This body
    opens and closes NO session scope, before or after this refactor -- it executes on the caller's
    session and the caller owns the boundary, which is the whole point of the no-commit core. Its two
    database touches are ``select_active_agent`` (step 1, BEFORE any S3 call) and the step-3 upsert;
    every extracted helper below either takes no session at all (:func:`_presign_multipart_parts`,
    :func:`_uploading_cloud_job_upsert`, :func:`_best_effort_abort_orphaned_multipart`) or only parks
    an in-memory record on ``session.info`` (:func:`_park_s3_enqueue`), so no S3 call moved across a
    transaction boundary and none newly sits inside one.
    """
    cfg = cast("ControlSettings", get_settings())

    # Gate on an online fileserver agent BEFORE mutating anything: with none available this is a
    # clean hold (NoActiveAgentError propagates) -- no multipart, no cloud_job, no parked enqueue.
    agent = await select_active_agent(session, kind="fileserver")

    # phaze-wz1q: fail loud BEFORE initiating a multipart upload that can never complete. file.file_size
    # is unvalidated agent wire input (schemas/agent_files.py:36-39 declines a storage-domain cap by
    # design); this is the point where it stops being a display value and becomes a loop bound, so it
    # gets re-bounded here rather than trusted as-is.
    if file.file_size > s3_staging.S3_MAX_OBJECT_SIZE_BYTES:
        raise s3_staging.S3StagingError(
            f"file {file.id} reports file_size={file.file_size} bytes, exceeding S3's "
            f"{s3_staging.S3_MAX_OBJECT_SIZE_BYTES}-byte max object size -- refusing to stage"
        )

    upload_id = await s3_staging.create_multipart_upload(file.id, bucket)
    try:
        parts = await _presign_multipart_parts(cfg, file, upload_id, bucket)

        # The ONE piece of database work in this body, deliberately left AT this call site rather than
        # inside a helper: it runs on the CALLER's session and the CALLER's transaction, and it sits
        # between the presign above and no S3 call at all below, exactly as it always has. Nothing in
        # this function opens or closes a session scope, so no transaction is held across S3 I/O that
        # was not already held across it by the caller.
        await session.execute(_uploading_cloud_job_upsert(file, upload_id, bucket))

        payload = UploadFileS3Payload(
            file_id=file.id,
            original_path=file.original_path,
            part_urls=parts.urls,
            part_size_bytes=parts.part_size_bytes,
            agent_id=agent.id,
        )
        queue = task_router.queue_for(agent.id, lane_for_task("s3_upload"))
        # phaze-grzo: PARK the enqueue -- do NOT fire it here. The caller flushes it AFTER committing the
        # cloud_job UPLOADING row so the job (and its report_uploaded callback) never precedes that row.
        # phaze-cws5: carry (file_id, upload_id, bucket) alongside it -- drop_pending_s3_enqueues is the
        # ONLY remaining compensation for a rollback that happens on the CALLER's side (a later candidate
        # poisoning the transaction, or the caller's own commit failing) rather than inside this core.
        _park_s3_enqueue(
            session,
            _PendingS3Enqueue(
                queue=queue,
                enqueue_kwargs={
                    "key": f"s3_upload:{file.id}",
                    # phaze-g37f: scale the SAQ job-net timeout with the part count so a multi-GB upload is
                    # not deterministically cancelled by a fixed single-part cap. Each part carries its own
                    # asyncio guard on the agent, so the net sits strictly above the SUM of those budgets.
                    "timeout": upload_file_saq_timeout_sec(parts.part_count),
                    # phaze-oj7x: pin retries EXPLICITLY to 0 (S3_UPLOAD_SAQ_RETRIES). Control (re-drive + reaper)
                    # is the sole re-drive vehicle; an unset retries would be clobbered to worker_max_retries by the
                    # before_enqueue hook, re-arming SAQ to replay the ORIGINAL payload against a multipart this very
                    # re-drive already aborted (guaranteed NoSuchUpload). See S3_UPLOAD_SAQ_RETRIES for the full note.
                    "retries": S3_UPLOAD_SAQ_RETRIES,
                    **payload.model_dump(mode="json"),
                },
                file_id=file.id,
                upload_id=upload_id,
                bucket=bucket,
            ),
        )
    except Exception:
        # Best-effort compensation (phaze-bbwx): upload_id is about to become unrecoverable (never
        # persisted, or the caller's transaction will roll back the row that would have persisted it),
        # so this is the only chance to abort it. The helper never raises, so a failed compensation
        # can never mask the ORIGINAL failure re-raised on the next line.
        await _best_effort_abort_orphaned_multipart(file.id, upload_id, bucket, orphaned_by="staging_failure")
        raise

    logger.info(
        "stage_file_to_s3: cloud_job staged + s3_upload enqueue parked (fires post-commit, phaze-grzo)",
        file_id=str(file.id),
        agent_id=agent.id,
        part_count=parts.part_count,
    )


def _bucket_ids_bound_to_backend(cfg: ControlSettings, backend_id: str) -> list[str]:
    """The bucket ids ``backend_id`` is bound to, or EMPTY when it names no backend carrying a bucket set.

    ``cfg.backends`` is a discriminated union over ``kind`` (``config_backends``) and only some kinds
    declare ``buckets`` at all, hence the ``getattr`` rather than an attribute read: a ``kind=local``
    backend and an unknown id are the same answer here -- nothing to re-pick over.

    Empty is the ONLY negative result, deliberately: it merges "no such backend" with "a backend with
    no bound bucket set" because :func:`_redrive_bucket` treats both identically, and keeping them
    apart would buy a distinction no caller reads.
    """
    backend = next((b for b in cfg.backends if b.id == backend_id and getattr(b, "buckets", None)), None)
    if backend is None:
        return []
    return list(getattr(backend, "buckets", []) or [])


def _redrive_bucket(cfg: ControlSettings, existing: CloudJob | None, file: FileRecord) -> BucketConfig | None:
    """Resolve the bucket a re-drive stages into: the RECORDED one, else a re-pick over the backend's set.

    A re-drive re-stages a file that already carries a ``cloud_job`` row, so its authoritative bucket is
    the recorded ``staging_bucket`` (MKUE-02 -- read it, never re-derive). Only when that column is absent
    (a legacy row staged before Phase 70, or a row whose backend later cleared it) does it fall back to
    re-picking deterministically over the file's backend's bound bucket set -- keeping the fresh multipart
    on the same D-06 bucket the presign/cleanup path will read.

    ``None`` now means ONE thing: a ``cloud_job`` row EXISTS but neither its ``staging_bucket`` nor its
    ``backend_id`` resolves to a configured bucket set -- a genuine bucket/configuration problem. The
    structurally different ``existing is None`` case (NO row at all, i.e. nothing was ever staged or the
    row was cleaned up after the work finished) is rejected by :func:`redrive_upload` BEFORE this is
    called, with :class:`NoCloudJobToRedriveError`. Merging the two behind one return value and one
    message is what made a completed file's stale ledger row read as a bucket/expiry failure for a
    month (phaze-k95r7); do not re-merge them.
    """
    if existing is None:
        return None
    if existing.staging_bucket:
        return s3_staging.resolve_bucket_config(cfg, existing.staging_bucket)
    if not existing.backend_id:
        return None
    bucket_ids = _bucket_ids_bound_to_backend(cfg, existing.backend_id)
    if not bucket_ids:
        return None
    return s3_staging.resolve_bucket_config(cfg, s3_staging.pick_bucket(file.id, bucket_ids))


async def redrive_upload(session: AsyncSession, file: FileRecord, task_router: AgentTaskRouter) -> None:
    """Abort the prior multipart (best-effort) and re-stage ``file`` with a fresh upload.

    Used by the Plan-04 ``/failed`` callback under the re-drive cap: a failed/abandoned upload
    leaves an in-flight multipart that must be aborted before a new one is initiated, so the prior
    attempt's bytes do not linger. The abort is best-effort (the upload may already be gone --- an
    eviction, a prior abort, a lifecycle sweep), so its failure never blocks the re-stage.

    Both the abort and the re-stage act on the RECORDED ``staging_bucket`` (MKUE-02) so the fresh
    multipart lands on exactly the bucket the presign/cleanup path reads back.

    TWO distinct refusals, deliberately not merged (phaze-k95r7):

    - **no ``cloud_job`` row at all** -> :class:`NoCloudJobToRedriveError`. There is no staging attempt
      to re-drive. Nothing is expired and nothing is mis-configured; the usual cause is that the work
      finished and the row was cleaned up, leaving a stale ledger row pointing at it.
    - **a row exists but no bucket resolves** -> :class:`~phaze.services.s3_staging.S3StagingError`.
      The genuine bucket/configuration problem, and the only one of the two that a later re-drive (or a
      config fix) can clear.
    """
    cfg = cast("ControlSettings", get_settings())
    existing = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    if existing is None:
        raise NoCloudJobToRedriveError(
            f"redrive_upload has no cloud_job row for {file.id} -- nothing was staged, so there is nothing to re-drive "
            "(the work is finished, or was never dispatched); this is NOT a bucket or expiry failure"
        )
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
