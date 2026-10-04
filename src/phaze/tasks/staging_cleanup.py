"""Expiration backstop for gateways lacking native S3 lifecycle support."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, text
import structlog

from phaze.config import get_settings
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services import s3_staging
from phaze.tasks.release_awaiting_cloud import _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.config_backends import BucketConfig


logger = structlog.get_logger(__name__)
_SAFE_STATUSES = frozenset({CloudJobStatus.AWAITING.value, CloudJobStatus.FAILED.value, CloudJobStatus.SUCCEEDED.value})
_BUCKET_SWEEP_TIMEOUT_SECONDS = 120


async def _cleanup_one(session: AsyncSession, candidate: s3_staging.StagingCleanupCandidate, bucket: BucketConfig, cutoff: datetime) -> bool:
    # Never wait behind a live admission tick. The global lock prevents re-staging,
    # while the ledger lock also excludes the upload-failure redrive path. Both are
    # transaction-scoped and released by the caller even on cancellation/S3 failure.
    locked = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
    if not locked:
        return False
    locked = await session.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(f"s3_upload:{candidate.file_id}"))))
    if not locked:
        return False
    row = (
        await session.execute(select(CloudJob).where(CloudJob.file_id == candidate.file_id).with_for_update(skip_locked=True))
    ).scalar_one_or_none()
    # Unknown/foreign ownership, all live statuses, and recently changed state fail
    # closed. Legacy objects without a recorded owner need operator investigation.
    if row is None or row.staging_bucket != bucket.id or row.status not in _SAFE_STATUSES:
        return False
    modified = row.updated_at or row.created_at
    modified = modified.replace(tzinfo=UTC) if modified.tzinfo is None else modified
    if modified >= cutoff or row.s3_key not in (None, s3_staging.staged_object_key(candidate.file_id)):
        return False
    return await s3_staging.cleanup_expired_staging(candidate, bucket, cutoff)


async def reap_expired_staging(ctx: dict[str, Any]) -> dict[str, int]:
    """Sweep only unsupported buckets, preserving active, recent and unknown ownership.

    Native lifecycle failures from auth/network errors do not silently select this
    fallback. Startup must have positively identified unsupported lifecycle APIs.
    Lists are read outside DB locks; each candidate gets fresh locked state plus an
    object-age recheck. Each bucket has a finite total budget and fails independently.
    """
    cfg = cast("ControlSettings", get_settings())
    cutoff = datetime.now(UTC) - timedelta(days=cfg.s3_lifecycle_ttl_days)
    fallback_ids = ctx.get("staging_lifecycle_fallback_buckets", set())
    tally = {"cleaned": 0, "preserved": 0, "failed_buckets": 0}
    for bucket in cfg.buckets:
        if bucket.id not in fallback_ids:
            continue
        try:
            async with asyncio.timeout(_BUCKET_SWEEP_TIMEOUT_SECONDS), aclosing(s3_staging.expired_staging_candidates(bucket, cutoff)) as candidates:
                async for candidate in candidates:
                    async with ctx["async_session"]() as session:
                        try:
                            cleaned = await _cleanup_one(session, candidate, bucket, cutoff)
                        finally:
                            await session.rollback()
                    tally["cleaned" if cleaned else "preserved"] += 1
        except Exception as exc:
            tally["failed_buckets"] += 1
            logger.warning(
                "staging expiration fallback failed; remaining objects preserved",
                bucket_id=bucket.id,
                error_type=type(exc).__name__,
                operation=getattr(exc, "operation_name", None),
                error_code=getattr(exc, "response", {}).get("Error", {}).get("Code"),
            )
    logger.info("staging expiration fallback completed", **tally)
    return tally
