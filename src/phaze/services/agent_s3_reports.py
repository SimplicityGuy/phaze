"""Control-side protocols for the agent S3-staging upload callbacks (Phase 53, Plan 04).

The control plane is the only place with the S3 credentials and the ORM, so the Postgres-free,
SDK-free file-server agent reports its multipart-upload outcome through the token-authed internal
callbacks in ``routers.agent_s3``. The control plane then COMPLETES the multipart upload itself
(KSTAGE-01/DIST-01 -- never the agent), flips the ``cloud_job`` state, and runs the bounded
re-drive / terminal-cleanup loop. This module owns those database/S3 state machines and returns
their decisions as typed outcomes; the router owns auth, schemas, logging and the HTTP contract.

Mirrors ``agent_push.py`` (report_pushed / report_push_mismatch):

- ``process_uploaded`` -- the agent reports the ordered ``(part_number, etag)`` list it collected
  from each part PUT. Control completes the multipart upload control-side, then flips ``cloud_job``
  ``UPLOADING -> UPLOADED`` with a rowcount guard. A duplicate/late callback (cloud_job already
  UPLOADED) is an idempotent no-op that does NOT re-complete the object (T-53-15).

- ``process_upload_failed`` -- the agent reports an upload failure. The ``s3_upload_attempt``
  counter rides the dedicated ``scheduling_ledger.redrive_attempt`` column keyed by
  ``s3_upload:<file_id>`` (phaze-y0j0: OUTSIDE the hook-rewritten ``payload`` JSONB so the bounded
  budget survives a crash mid-re-drive). Under ``push_max_attempts`` control re-drives the upload
  (``cloud_staging.redrive_upload``: abort the prior multipart + re-stage) keeping ``cloud_job``
  UPLOADING and stamps the incremented attempt back (T-53-16). At/over the cap control spills
  ``cloud_job`` back to ``awaiting`` + clears the ledger (committed FIRST), THEN aborts the
  multipart + deletes the staged object POST-COMMIT (phaze-1v37: best-effort cleanup that holds no
  lock across the S3 round-trip; lifecycle TTL backstops a miss) -- the terminal cleanup that
  prevents orphaned in-flight uploads / leaked objects (KSTAGE-04 / T-53-17). With no fileserver
  online the re-drive is a clean HELD outcome (NoActiveAgentError caught), never a raise (T-53-19).

phaze-1v37 (the transaction-boundary invariant this module exists to keep explicit): every
control-side S3 SDK call runs OUTSIDE the request's DB transaction -- the success protocol releases
the (lock-free) read before ``complete_multipart_upload`` and re-opens a transaction for the
idempotent CAS; the failure protocol commits the spill CAS + ledger clear before the abort/delete
cleanup -- so a wedged S3 endpoint never pins a pooled connection idle-in-transaction (draining the
small PgBouncer pool and 500ing the control plane). The S3 client is additionally bounded with
explicit connect/read timeouts (``s3_client_timeout_sec``). The under-cap ``redrive_upload`` S3
setup still runs under the attempt-counter advisory lock (its ledger RMW is coupled to that lock);
the bounded client timeouts cap that residual pin.

Three orderings here are load-bearing and are each annotated at their guard below: advisory lock
NOT row lock (D-11 / T-83-02), the CAS pinned to the captured ``upload_id`` (phaze-p8h3), and
commit-before-network / commit-before-enqueue (phaze-1v37, phaze-0vuf).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services import cloud_staging, s3_staging
from phaze.services.backends import hold_awaiting_cloud, resolved_non_local_kind
from phaze.services.enqueue_router import NoActiveAgentError
from phaze.services.scheduling_ledger import clear_ledger_entry
from phaze.tasks.submit_cloud_job import submit_cloud_job_key


if TYPE_CHECKING:
    import uuid

    from phaze.config import ControlSettings
    from phaze.config_backends import BucketConfig
    from phaze.services.agent_task_router import AgentTaskRouter


class ProtocolOutcome(StrEnum):
    """Stable high-level outcomes shared by both callback protocols."""

    COMPLETED = "completed"
    HELD = "held"
    NOOP = "noop"
    SPILLED = "spilled"


class UploadedReason(StrEnum):
    """Precise success-callback decisions used by router logging."""

    ABSENT_OR_LATE = "absent_or_late"
    EMPTY_PARTS = "empty_parts"
    ENQUEUE_FAILED = "enqueue_failed"
    MULTIPART_COMPLETED = "multipart_completed"
    SUBMIT_ROUTED = "submit_routed"
    UPLOAD_ID_CAS_MISS = "upload_id_cas_miss"


class UploadFailedReason(StrEnum):
    """Precise failure-callback decisions used by router logging."""

    NO_AGENT = "no_agent"
    OVER_CAP_CAS_MISS = "over_cap_cas_miss"
    REDRIVEN = "redriven"
    SPILLED = "spilled"
    STAGING_UNAVAILABLE = "staging_unavailable"
    UNDER_CAP_LATE = "under_cap_late"


@dataclass(frozen=True)
class UploadedResult:
    """Typed result of the upload-success protocol."""

    outcome: ProtocolOutcome
    reason: UploadedReason
    cleanup_error: BaseException | None = None
    enqueue_error: BaseException | None = None


@dataclass(frozen=True)
class UploadFailedResult:
    """Typed result of the upload-failure protocol."""

    outcome: ProtocolOutcome
    reason: UploadFailedReason
    attempt: int
    cap: int
    cleanup_error: BaseException | None = None
    hold_error: BaseException | None = None

    @property
    def cleared(self) -> bool:
        """Whether this callback performed the terminal spill and cleanup transition."""
        return self.outcome is ProtocolOutcome.SPILLED


class UnresolvableStagingBucketError(RuntimeError):
    """The persisted upload names a staging bucket absent from the current registry."""


class UnknownUploadFileError(LookupError):
    """An under-cap failure callback names no source file."""


ResolveQueue = Callable[[str, Any, AsyncSession], Awaitable[Any]]


async def _best_effort_cleanup(
    file_id: uuid.UUID,
    upload_id: str | None,
    bucket: BucketConfig,
) -> BaseException | None:
    """Abort the multipart and delete the object, returning rather than raising cleanup faults.

    Callers MUST have committed their state transition before calling this (phaze-1v37): the abort
    + delete run POST-COMMIT so no transaction or row lock is held across the S3 round-trip, and the
    lifecycle TTL backstops a miss (KSTAGE-04 / T-53-17), mirroring ``_delete_staged_object_if_cloud``'s
    record-first discipline (phaze-uoiw).

    phaze-z0eur: the fault is CAUGHT and returned, never raised -- the transition the caller just
    committed is ALREADY durable, so an S3 fault here (e.g. a 503 SlowDown mid-burst) must not turn a
    successful, already-committed transition into an unhandled 500 the agent retries against a no-op.
    Bare ``Exception`` mirrors ``drop_pending_s3_enqueues`` / ``stage_file_to_s3``'s compensation
    catches, which also see a raw network/DNS error surface before the SDK call reaches botocore's
    ``ClientError`` wrapping.
    """
    try:
        if upload_id:
            await s3_staging.abort_multipart_upload(file_id, upload_id, bucket)
        await s3_staging.delete_staged_object(file_id, bucket)
    except Exception as exc:
        return exc
    return None


async def process_uploaded(
    session: AsyncSession,
    file_id: uuid.UUID,
    parts: list[tuple[int, str]],
    settings: ControlSettings,
    app_state: Any,
    resolve_queue: ResolveQueue,
) -> UploadedResult:
    """Complete a multipart upload and advance its generation-pinned cloud job.

    No pooled connection is held across multipart completion. The fresh CAS is pinned to both
    ``UPLOADING`` and the captured upload id, so a concurrent redrive cannot be mistaken for the
    generation this callback completed.
    """
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    # No staging row, already past UPLOADING (completed/failed), or no multipart to complete:
    # idempotent no-op, never re-complete (T-53-15). The router renders this as a 200.
    if cloud_job is None or cloud_job.status != CloudJobStatus.UPLOADING.value or cloud_job.upload_id is None:
        return UploadedResult(ProtocolOutcome.NOOP, UploadedReason.ABSENT_OR_LATE)

    # phaze-eo5x: an EMPTY parts list is a degenerate/zero-byte upload (the agent's _transfer_parts
    # returns [] for a 0-byte source: the first read yields b'' and breaks before any PUT). S3 multipart
    # REQUIRES >=1 part -- CompleteMultipartUpload rejects an empty list with MalformedXML, which
    # s3_staging re-raises as S3StagingError (it swallows only NoSuchUpload/404). Nothing catches it, so
    # it escaped as an unhandled 500 that the SAQ retry reproduced forever, permanently stranding
    # cloud_job UPLOADING and leaking the in-flight cap slot + the multipart. There is NO valid completion
    # for zero parts, so drive a clean terminal resolution that FREES the job instead: spill the cloud_job
    # back to 'awaiting' with its cloud budget SPENT (so select_backend routes the file to LOCAL, where a
    # 0-byte file terminally fails analysis -- the uniform failure funnel), abort the orphaned multipart +
    # delete any staged object, and clear the ledger -- the SAME terminal cleanup the over-cap failure
    # branch runs (KSTAGE-04 / T-53-17). The router still returns a definitive 200 so the agent stops retrying.
    if not parts:
        bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket)
        # NULL-GUARD: hold_awaiting_cloud's spill CAS dereferences file.id (mirrors the over-cap spill
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
        cleanup_error = await _best_effort_cleanup(file_id, upload_id, bucket) if cleared and bucket is not None else None
        return UploadedResult(
            ProtocolOutcome.SPILLED if cleared else ProtocolOutcome.NOOP,
            UploadedReason.EMPTY_PARTS,
            cleanup_error=cleanup_error,
        )

    # Complete the multipart upload control-side with the agent-reported parts (KSTAGE-01), on the
    # RECORDED staging bucket (MKUE-02 -- a kueue UPLOADING row always carries the staging_bucket
    # KueueBackend.dispatch stamped; resolve it, never re-derive). An unresolvable bucket is the
    # router's 409.
    bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket)
    if bucket is None:
        raise UnresolvableStagingBucketError

    # phaze-1v37: capture the ORM values needed for the S3 call into locals, then RELEASE the read
    # transaction BEFORE the multipart-complete round-trip so the pooled connection is not pinned
    # idle-in-transaction across S3 network I/O (a wedged/blackholed endpoint would otherwise hold a
    # PgBouncer SESSION-mode upstream connection for the full botocore window, and a handful of
    # concurrent callbacks would drain the small pool and 500 the whole control plane). The SELECT above
    # took NO row lock (READ COMMITTED) and wrote nothing, so committing here just returns the connection
    # to the pool. The idempotent UPLOADING->UPLOADED CAS below re-opens a fresh transaction after the
    # S3 call, and its rowcount guard still makes a concurrent duplicate a clean no-op.
    upload_id = cloud_job.upload_id
    await session.commit()
    await s3_staging.complete_multipart_upload(file_id, upload_id, parts, bucket)

    # Idempotent flip guarded on the CURRENT status so a concurrent duplicate that also passed the
    # pre-check above does not double-flip. An UPDATE returns a CursorResult at runtime (exposing
    # rowcount); the async stubs type it as the base Result, so cast to read the affected-row count.
    #
    # phaze-p8h3: ALSO pin the CAS to the SAME upload_id captured above. complete_multipart_upload
    # swallows NoSuchUpload as "already assembled" (the documented retry-after-DB-failure case), but
    # NoSuchUpload is ambiguous -- it is also what S3 returns for an ABORTED upload. Nothing locks the
    # row between the pre-check and this CAS (the transaction was committed above precisely to avoid
    # pinning a connection across the S3 round-trip), so a concurrent under-cap re-drive
    # (cloud_staging.redrive_upload) can abort THIS upload_id and stamp a fresh one while keeping
    # cloud_job UPLOADING. Without the upload_id predicate, complete's silent "success" on the aborted
    # upload would still flip the row to UPLOADED (status alone still matches) for an object that was
    # never assembled. Requiring the row's upload_id to still equal the one we just completed makes a
    # concurrent re-drive's fresh upload_id fail the CAS instead -- a clean no-op mirroring a lost race.
    res = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CloudJob)
            .where(CloudJob.file_id == file_id, CloudJob.status == CloudJobStatus.UPLOADING.value, CloudJob.upload_id == upload_id)
            .values(status=CloudJobStatus.UPLOADED.value)
        ),
    )
    if res.rowcount == 0:
        await session.commit()
        return UploadedResult(ProtocolOutcome.NOOP, UploadedReason.UPLOAD_ID_CAS_MISS)

    # Phase 55 (D-01b, KROUTE-03): on the kueue target the upload-complete callback is also the
    # post-staging seam -- it enqueues submit_cloud_job through enqueue_router on the controller queue
    # (NEVER a raw enqueue -- KROUTE-04). A1 uses rsync and never reaches these S3 callbacks, so the
    # resolved-kind == "kueue" guard is defensive: a non-kueue target preserves today's cloud_job-only
    # behavior. Phase 68 (D-09): registry-derived kind via the Backend registry helper (was the retired
    # <=1-non-local accessor).
    if resolved_non_local_kind(settings) == "kueue":
        # Phase 90 (D-09): the companion FileRecord PUSHING -> PUSHED CAS flip this seam used to perform
        # was removed (read + write deleted atomically in PR-B). Idempotency is carried solely by the
        # OUTER cloud_job CAS above (UPLOADING -> UPLOADED; rowcount==0 already returned) PLUS the
        # deterministic submit_cloud_job key -- a duplicate/late callback is already a no-op at the
        # cloud_job sidecar (the sole derived authority PR-A reads), so no state guard is load-bearing.
        #
        # phaze-0vuf: COMMIT the UPLOADING -> UPLOADED CAS BEFORE enqueuing submit_cloud_job.
        # resolve_queue_for_task returns the controller's own SAQ PostgresQueue, which enqueues on
        # ITS OWN psycopg pool and commits the job durably and IMMEDIATELY -- independent of THIS
        # asyncpg session's commit boundary (the same class the phaze-grzo / phaze-v40v fixes closed
        # for cloud_staging and agent_push). Enqueuing first meant a subsequent commit failure (request
        # cancellation, a PgBouncer blip) rolled the UPLOADED flip back while a real Kueue Job had
        # already been submitted for it: submit_cloud_job's own upsert then reads the still-'uploading'
        # row, misses its `where=status IN ('uploaded','submitted')` guard, rolls back, and deletes the
        # Job it just created -- a real k8s Job created and destroyed for nothing, and the file stuck
        # UPLOADING until the age-bounded stranded-staging reaper spills it (discarding the fully
        # staged object). Committing first makes a failure here benign: nothing is dispatched, the
        # cloud_job stays durably UPLOADED, and the file re-enters the drain on the next tick.
        await session.commit()

        # Route submit_cloud_job onto the CONTROLLER queue via the single Phase-30 seam (never a raw
        # controller_queue.enqueue / the default queue -- KROUTE-04, T-55-SEAM-03). Deterministic key
        # dedups a replayed submit (KSUBMIT-01). submit_cloud_job stays staging-free (rejected coupling).
        # Post-commit: a failed enqueue (controller pool down) is best-effort -- the control state is
        # already correct + durable (cloud_job UPLOADED), so HELD is logged loudly and the router still
        # returns 200 rather than 500 (mirrors report_pushed's post-commit process_file enqueue).
        try:
            routed = await resolve_queue("submit_cloud_job", app_state, session)
            await routed.queue.enqueue("submit_cloud_job", key=submit_cloud_job_key(file_id), file_id=str(file_id))
        except Exception as exc:
            return UploadedResult(ProtocolOutcome.HELD, UploadedReason.ENQUEUE_FAILED, enqueue_error=exc)
        return UploadedResult(ProtocolOutcome.COMPLETED, UploadedReason.SUBMIT_ROUTED)

    await session.commit()
    return UploadedResult(ProtocolOutcome.COMPLETED, UploadedReason.MULTIPART_COMPLETED)


async def _spill_failed_upload(
    session: AsyncSession,
    file_id: uuid.UUID,
    ledger_key: str,
    settings: ControlSettings,
    next_attempt: int,
) -> UploadFailedResult:
    """Conditionally spill an over-cap upload, then clean S3 after committing durable state.

    CAS-guarded terminal spill (D-09/D-10/D-03) + cleanup + ledger clear. D-01/D-02: the spill
    re-stamp is routed through the SINGLE awaiting writer (``services.backends.hold_awaiting_cloud``)
    instead of an inline CAS. Its spill branch preserves the exact shipped guard: D-09 anchors on
    ``cloud_job.status IN ('uploading','uploaded')`` (the sidecar is the single CAS domain, NOT
    ``FileRecord.state``; SC#1); D-03 re-stamps the row to ``status='awaiting'`` (was FAILED) with
    attempts SPENT (>= ``cloud_submit_max_attempts``) so ``select_backend`` routes the spilled file to
    LOCAL; ``clear_cloud_phase=True`` nulls ``cloud_phase`` (WR-01, off the "Running" tile, D-12). It
    returns False (a full no-op) when a late/duplicate failure callback matches an already-advanced row
    (running/succeeded) -- the historical clobber stays closed (SC#2 / T-83-01).
    """
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
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
        # seams that own _delete_staged_object_if_cloud), NO ledger clear. Commit and report cleared=False
        # (mirrors report_push_mismatch's over-cap no-op exactly).
        await session.commit()
        return UploadFailedResult(
            ProtocolOutcome.NOOP,
            UploadFailedReason.OVER_CAP_CAS_MISS,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
        )

    # cleared (helper CAS hit): gate S3 cleanup + ledger clear behind the CAS.
    # Phase 90 (D-09): the former AWAITING_CLOUD FileRecord.state dual-write was removed; the cloud_job
    # sidecar re-stamped to 'awaiting' by hold_awaiting_cloud is the sole derived authority.
    # MKUE-02: act on the RECORDED staging bucket; a bucketless row (no S3 object) skips the S3 ops cleanly.
    # phaze-1v37: capture the bucket + upload_id into locals and COMMIT the spill CAS + ledger clear
    # FIRST, then run the S3 abort + delete AFTER the commit. Pre-1v37 they ran while the transaction
    # (holding the pg_advisory_xact_lock taken at the top AND the cloud_job row lock the spill CAS took)
    # was still open, pinning the pooled connection across the S3 round-trip and blocking the staging
    # reaper + sibling failure callbacks for the full botocore window.
    bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket) if cloud_job is not None else None
    upload_id = cloud_job.upload_id if cloud_job is not None else None
    await clear_ledger_entry(session, ledger_key)
    await session.commit()
    cleanup_error = await _best_effort_cleanup(file_id, upload_id, bucket) if bucket is not None else None
    return UploadFailedResult(
        ProtocolOutcome.SPILLED,
        UploadFailedReason.SPILLED,
        attempt=next_attempt,
        cap=settings.push_max_attempts,
        cleanup_error=cleanup_error,
    )


async def process_upload_failed(
    session: AsyncSession,
    file_id: uuid.UUID,
    settings: ControlSettings,
    task_router: AgentTaskRouter,
) -> UploadFailedResult:
    """Serialize, re-drive, or terminally spill a failed multipart upload (KSTAGE-04).

    The ``s3_upload_attempt`` counter lives in the dedicated ``scheduling_ledger.redrive_attempt``
    column keyed by ``s3_upload:<file_id>`` (phaze-y0j0: OUTSIDE the hook-rewritten ``payload`` JSONB
    so the bounded budget survives a crash in the re-drive->stamp window). Read it (default 0) and
    increment:

    - ``attempt + 1 > push_max_attempts`` -> spill ``cloud_job`` to awaiting + abort the multipart +
      delete the staged object + clear the ledger: the terminal cleanup that prevents an orphaned
      in-flight upload / leaked object (KSTAGE-04 / T-53-17).
    - otherwise -> re-drive the upload (``cloud_staging.redrive_upload``: abort the prior multipart +
      re-stage with a fresh upload) keeping ``cloud_job`` UPLOADING, and stamp the incremented attempt
      back on the ledger row (T-53-16). With no fileserver online this is a clean HELD outcome
      (NoActiveAgentError caught) the router renders as a 200, never a 500 (T-53-19).
    """
    ledger_key = f"s3_upload:{file_id}"

    # D-11 (T-83-02): serialize the s3_upload_attempt read->+1->write-back so two concurrent failure
    # callbacks can't both read the same counter and lose an increment (letting a file exceed its bounded
    # upload budget). A transaction-scoped ADVISORY lock keyed by the ledger key -- NOT a
    # `.with_for_update()` row lock: the under-cap path re-enqueues the s3_upload job via redrive_upload
    # -> stage_file_to_s3 while THIS transaction is still open, and s3_upload is a registered
    # before_enqueue key-builder, so apply_deterministic_key upserts THIS SAME ledger row from its OWN
    # session (ON CONFLICT DO UPDATE). A row lock we hold would self-deadlock that nested write (no
    # statement_timeout to break it, no Postgres deadlock cycle to detect); the advisory lock lives in a
    # different lock space, so the hook's upsert never blocks on it and a second concurrent callback waits
    # on the advisory lock until we commit -- the RMW is serialized and each increment is applied exactly
    # once (mirrors agent_push.py).
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(ledger_key))))

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key))).scalar_one_or_none()
    current_attempt = int(row.redrive_attempt) if row is not None and row.redrive_attempt is not None else 0
    next_attempt = current_attempt + 1

    if next_attempt > settings.push_max_attempts:
        return await _spill_failed_upload(session, file_id, ledger_key, settings, next_attempt)

    # Under the cap: re-drive the upload, keeping the cloud_job UPLOADING. Load the FileRecord by the
    # PATH file_id (AUTH-01) so redrive_upload has the source path / size; an unknown file_id with a
    # re-drive request is malformed -> the router's 404 (mirrors the presign-download load, 53-02).
    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
    if file is None:
        raise UnknownUploadFileError

    # phaze-0deg: guard the under-cap re-drive on cloud_job.status == UPLOADING, mirroring the over-cap
    # branch's CAS above (and the success protocol's "absent or not UPLOADING" no-op). Without this a
    # late/duplicate failure callback -- including one that lands after the reaper (phaze-ul2v) already
    # spilled this row to 'awaiting' and cleared its ledger entry, resetting next_attempt back to 1 --
    # would unconditionally clobber an ADVANCED row (UPLOADED / SUBMITTED / RUNNING / already-spilled
    # 'awaiting') back to UPLOADING via redrive_upload's unconditional upsert (cloud_staging's
    # on_conflict_do_update has no ``where=`` predicate), re-consuming a kueue cap slot with a fresh
    # multipart, 409ing the live pod's presign download, and burning a redundant re-drive attempt from
    # the bounded budget.
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    if cloud_job is None or cloud_job.status != CloudJobStatus.UPLOADING.value:
        await session.commit()
        return UploadFailedResult(
            ProtocolOutcome.NOOP,
            UploadFailedReason.UNDER_CAP_LATE,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
        )

    try:
        await cloud_staging.redrive_upload(session, file, task_router)
    except NoActiveAgentError:
        # No fileserver online: leave the cloud_job UPLOADING for a later re-drive; clean 200 hold.
        await session.commit()
        return UploadFailedResult(
            ProtocolOutcome.HELD,
            UploadFailedReason.NO_AGENT,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
        )
    except s3_staging.S3StagingError as exc:
        # phaze-kuhbu: redrive_upload's OWN setup leg raises S3StagingError on two reachable paths --
        # NoCloudJobToRedriveError (a subclass) when the cloud_job row vanished, or the base class when
        # the recorded staging_bucket no longer resolves (an operator removed it) or create_multipart_upload
        # hits the very S3 outage that made the agent's PUT fail and report the failure in the first place.
        # All three legs raise BEFORE _stage_file_to_s3 parks an s3_upload enqueue, so there is nothing to
        # drop -- this is the SAME clean 200 hold the NoActiveAgentError branch above already takes,
        # matching the endpoint's documented never-500 posture (T-53-19) instead of 500ing the agent AND
        # losing the redrive_attempt stamp for every failure callback during the outage.
        await session.commit()
        return UploadFailedResult(
            ProtocolOutcome.HELD,
            UploadFailedReason.STAGING_UNAVAILABLE,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
            hold_error=exc,
        )

    # Stamp the incremented attempt into the DEDICATED `redrive_attempt` column. redrive_upload ->
    # stage_file_to_s3 commits a FRESH payload (new presigned part_urls) to THIS same ledger row via
    # its enqueue hook, from its own session, BEFORE control returns here. Because the counter now
    # lives in `redrive_attempt` (a column the hook's ON CONFLICT DO UPDATE never touches) and NOT in
    # `payload`, this stamp: (1) cannot clobber the hook's fresh part_urls -- the old WR-02 re-fetch
    # dance is unnecessary; and (2) if a crash lands between the hook's commit and this commit, the
    # column keeps its prior value (un-incremented at `current_attempt`) instead of being reset to 0,
    # so the bounded upload budget survives the crash window (phaze-y0j0).
    #
    # phaze-hi3ix: wrap the stamp + commit in try/except BaseException so a failure here still drops
    # the s3_upload enqueue redrive_upload just parked on the session -- mirroring stage_file_to_s3's own
    # `except BaseException: await drop_pending_s3_enqueues(session); raise`. Without this, a failure at
    # either the UPDATE or the commit let the request-scoped session be discarded with the parked entry
    # silently GC'd, skipping the phaze-cws5 abort compensation for the FRESH multipart redrive_upload
    # just created -- since that multipart's upload_id was never persisted anywhere (the redrive's own
    # cloud_job upsert rolled back too), no later cleanup path could ever find it to abort it. The
    # explicit rollback puts the aborted-transaction session back into a usable state before the S3-only
    # drop call (which issues no SQL of its own).
    try:
        await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == ledger_key).values(redrive_attempt=next_attempt))
        await session.commit()
    except BaseException:
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise
    # phaze-grzo: redrive_upload PARKS its fresh s3_upload enqueue on the session; fire it ONLY now
    # that the re-driven cloud_job (still UPLOADING) and the attempt stamp are durably committed, so the
    # re-driven job (and its success callback) can never precede the committed row it reads. A commit
    # failure above raises before this line, so the parked enqueue is never fired against a rolled-back
    # row (the request-scoped session is discarded, dropping the parked entry).
    await cloud_staging.flush_pending_s3_enqueues(session)

    return UploadFailedResult(
        ProtocolOutcome.COMPLETED,
        UploadFailedReason.REDRIVEN,
        attempt=next_attempt,
        cap=settings.push_max_attempts,
    )
