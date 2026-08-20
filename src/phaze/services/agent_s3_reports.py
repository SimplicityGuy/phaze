"""Control-side protocols for agent S3 upload callbacks.

The HTTP router owns authentication, schemas, logging, and response construction. This module owns
the delicate database/S3 state machines and exposes their decisions as typed outcomes. Keeping the
protocol here makes the transaction boundaries explicit without hiding them behind a repository
abstraction: reads are committed before S3 completion, state is committed before externally visible
enqueues, and terminal spill state is committed before best-effort S3 cleanup.
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
    """Abort the multipart and delete the object, returning rather than raising cleanup faults."""
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
    if cloud_job is None or cloud_job.status != CloudJobStatus.UPLOADING.value or cloud_job.upload_id is None:
        return UploadedResult(ProtocolOutcome.NOOP, UploadedReason.ABSENT_OR_LATE)

    if not parts:
        bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket)
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

    bucket = s3_staging.resolve_bucket_config(settings, cloud_job.staging_bucket)
    if bucket is None:
        raise UnresolvableStagingBucketError

    upload_id = cloud_job.upload_id
    # Release the read-only transaction before the network call. This ordering is load-bearing.
    await session.commit()
    await s3_staging.complete_multipart_upload(file_id, upload_id, parts, bucket)

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

    if resolved_non_local_kind(settings) == "kueue":
        # Make the state visible before an independently committed queue enqueue can run.
        await session.commit()
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
    """Conditionally spill an over-cap upload, then clean S3 after committing durable state."""
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
    cleared = file is not None and await hold_awaiting_cloud(
        session,
        file,
        attempts=settings.cloud_submit_max_attempts,
        expect_status=(CloudJobStatus.UPLOADING.value, CloudJobStatus.UPLOADED.value),
        clear_cloud_phase=True,
    )
    if not cleared:
        await session.commit()
        return UploadFailedResult(
            ProtocolOutcome.NOOP,
            UploadFailedReason.OVER_CAP_CAS_MISS,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
        )

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
    """Serialize, re-drive, or terminally spill a failed multipart upload."""
    ledger_key = f"s3_upload:{file_id}"

    # Advisory rather than row locking is intentional: the nested enqueue hook upserts this same
    # ledger row from another session, so a row lock here would self-deadlock.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(ledger_key))))

    row = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == ledger_key))).scalar_one_or_none()
    current_attempt = int(row.redrive_attempt) if row is not None and row.redrive_attempt is not None else 0
    next_attempt = current_attempt + 1

    if next_attempt > settings.push_max_attempts:
        return await _spill_failed_upload(session, file_id, ledger_key, settings, next_attempt)

    file = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one_or_none()
    if file is None:
        raise UnknownUploadFileError

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
        await session.commit()
        return UploadFailedResult(
            ProtocolOutcome.HELD,
            UploadFailedReason.NO_AGENT,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
        )
    except s3_staging.S3StagingError as exc:
        await session.commit()
        return UploadFailedResult(
            ProtocolOutcome.HELD,
            UploadFailedReason.STAGING_UNAVAILABLE,
            attempt=next_attempt,
            cap=settings.push_max_attempts,
            hold_error=exc,
        )

    try:
        await session.execute(update(SchedulingLedger).where(SchedulingLedger.key == ledger_key).values(redrive_attempt=next_attempt))
        await session.commit()
    except BaseException:
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise
    await cloud_staging.flush_pending_s3_enqueues(session)

    return UploadFailedResult(
        ProtocolOutcome.COMPLETED,
        UploadFailedReason.REDRIVEN,
        attempt=next_attempt,
        cap=settings.push_max_attempts,
    )
