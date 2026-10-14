"""Bounded durable expansion and execution. Queue delivery is recoverable from persisted intent."""

from datetime import timedelta
import hashlib
from typing import Any
import uuid

from sqlalchemy import and_, false, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from phaze.models.file import FileRecord
from phaze.models.provider_source import ProviderRecordingSelection
from phaze.models.remote_lookup_batch import RemoteLookupBatch, RemoteLookupMembership, RemoteLookupWork
from phaze.models.remote_transport import RemoteTransportAttempt
from phaze.schemas.remote_lookup import LookupCommand
from phaze.services.remote_lookup import RemoteLookup, _recording_context, _searchable


logger = structlog.get_logger(__name__)

CHUNK = 100
ACTIVE = ("queued", "running", "deferred", "blocked")


async def eligibility(session: AsyncSession, media_id: uuid.UUID) -> str | None:
    if await session.scalar(
        select(ProviderRecordingSelection.media_id).where(
            ProviderRecordingSelection.media_id == media_id, ProviderRecordingSelection.kind == "tracklist"
        )
    ):
        return "reviewed_selection"
    try:
        context = await _recording_context(session, media_id)
    except ValueError:
        return "recording_unavailable"
    return None if _searchable(context) else "no_usable_hint"


async def create_batch(sessions: async_sessionmaker[AsyncSession], service: RemoteLookup) -> uuid.UUID:
    providers = [status.provider_id for status in await service.provider_status() if status.enabled]
    async with sessions() as session, session.begin():
        upper = await session.scalar(select(FileRecord.id).order_by(FileRecord.id.desc()).limit(1))
        batch = RemoteLookupBatch(providers=providers, upper_bound=upper, expanded=not providers or upper is None)
        session.add(batch)
        await session.flush()
        return batch.id


async def expand_batch(sessions: async_sessionmaker[AsyncSession], batch_id: uuid.UUID) -> bool:
    """Commit one keyset chunk and its cursor/memberships together; competing expanders serialize."""
    async with sessions() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(728513929)"))
        batch = await session.scalar(select(RemoteLookupBatch).where(RemoteLookupBatch.id == batch_id).with_for_update())
        if batch is None or batch.expanded:
            return True
        query = select(FileRecord.id).where(FileRecord.id <= batch.upper_bound).order_by(FileRecord.id).limit(CHUNK)
        if batch.cursor is not None:
            query = query.where(FileRecord.id > batch.cursor)
        ids = list(await session.scalars(query))
        for media_id in ids:
            if await eligibility(session, media_id):
                batch.excluded += 1
            else:
                for provider_id in batch.providers:
                    existing = await session.scalar(
                        select(RemoteLookupWork).where(
                            RemoteLookupWork.media_id == media_id, RemoteLookupWork.provider_id == provider_id, RemoteLookupWork.status.in_(ACTIVE)
                        )
                    )
                    if existing is None:
                        existing = RemoteLookupWork(media_id=media_id, provider_id=provider_id)
                        session.add(existing)
                        await session.flush()
                        batch.selected += 1
                    else:
                        batch.existing += 1
                    await session.execute(insert(RemoteLookupMembership).values(batch_id=batch.id, work_id=existing.id).on_conflict_do_nothing())
            batch.cursor = media_id
        batch.expanded = len(ids) < CHUNK
        return batch.expanded


async def execute_work(sessions: async_sessionmaker[AsyncSession], service: RemoteLookup, work_id: uuid.UUID) -> str:
    """Short claim/read/write transactions; no transaction or sleeping worker across pacing."""
    now = service._clock.wall()
    async with sessions() as session, session.begin():
        work = await session.scalar(select(RemoteLookupWork).where(RemoteLookupWork.id == work_id).with_for_update())
        if work is None or work.status not in ACTIVE:
            return "inactive"
        if work.lease_until is not None and work.lease_until > now:
            return "busy"
        if work.not_before is not None and work.not_before > now:
            return "deferred"
        reason = await eligibility(session, work.media_id)
        if reason:
            work.status, work.code = "completed", reason
            return reason
        context = await _recording_context(session, work.media_id)
        digest = hashlib.sha256(context.model_dump_json().encode()).hexdigest()
        if work.context_digest is not None and work.context_digest != digest:
            # Replaying successful pages against changed hints would mix distinct discoveries.
            work.status, work.code = "completed", "recording_metadata_changed"
            return work.code
        work.context_digest = digest
        work.status = "running"
        work.lease_until = now + timedelta(seconds=120)
        media_id, provider_id = work.media_id, work.provider_id
    try:
        result = await service.queued_lookup(LookupCommand(media_id=media_id, provider_id=provider_id), work_id)
        outcomes = (result.outcome, *result.warnings)
        retry = next((item for item in outcomes if item.status == "retry"), None)
        blocked = next(
            (
                item
                for item in outcomes
                if item.code
                in {
                    "transport_stopped",
                    "robots_disallow",
                    "challenge",
                    "http_403",
                    "http_429",
                    "disabled_provider",
                    "credential_missing",
                    "run_budget_exhausted",
                }
            ),
            None,
        )
        status = "blocked" if blocked else ("deferred" if retry else "completed")
        code = (blocked or retry or result.outcome).code
        payload = result.model_dump(mode="json")
        delay = max(1.0, retry.retry_after_seconds or 60.0) if retry else None
        transient = retry is not None and retry.code not in {"paced", "durable_budget_exhausted"}
    except Exception as exc:
        logger.warning("remote_lookup.worker_error", error_type=type(exc).__name__)
        status, code, payload, delay, transient = "deferred", "worker_error", None, 60.0, True
    async with sessions() as session, session.begin():
        work = await session.get(RemoteLookupWork, work_id, with_for_update=True)
        if work is None:
            return "deleted"
        if transient:
            work.failures += 1
            if work.failures >= 5:
                status, delay = "failed", None
        work.status, work.code, work.result = status, code, payload
        work.lease_until = None
        work.not_before = service._clock.wall() + timedelta(seconds=delay) if delay else None
    return status


async def dispatch_pending(sessions: async_sessionmaker[AsyncSession], service: RemoteLookup, queue: Any) -> None:
    """Recovery cron only redelivers operator-persisted intent; it never creates new discovery scope."""
    async with sessions() as session:
        batches = list(
            await session.scalars(
                select(RemoteLookupBatch).where(RemoteLookupBatch.expanded.is_(False)).order_by(RemoteLookupBatch.updated_at).limit(CHUNK)
            )
        )
    for batch in batches:
        await queue.enqueue("expand_remote_lookup_batch", batch_id=str(batch.id), key=f"remote-expand:{batch.id}:{batch.cursor}")
    now = service._clock.wall()
    statuses = {status.provider_id: status for status in await service.provider_status()}
    available = [provider_id for provider_id, status in statuses.items() if status.available and not status.stopped]
    usage = select(func.count()).where(RemoteTransportAttempt.run_id == RemoteLookupWork.id).scalar_subquery()
    unblocked = (
        or_(
            *(
                and_(
                    RemoteLookupWork.provider_id == provider_id,
                    or_(RemoteLookupWork.code != "run_budget_exhausted", usage < statuses[provider_id].max_requests_per_run),
                )
                for provider_id in available
            )
        )
        if available
        else false()
    )
    async with sessions() as session:
        jobs = list(
            await session.scalars(
                select(RemoteLookupWork)
                .where(
                    RemoteLookupWork.status.in_(ACTIVE),
                    or_(RemoteLookupWork.status != "blocked", unblocked),
                    or_(RemoteLookupWork.not_before.is_(None), RemoteLookupWork.not_before <= now),
                    or_(RemoteLookupWork.lease_until.is_(None), RemoteLookupWork.lease_until <= now),
                )
                .order_by(RemoteLookupWork.updated_at)
                .limit(CHUNK)
            )
        )
    for work in jobs:
        provider = statuses.get(work.provider_id)
        if (
            provider is not None
            and work.status == "blocked"
            and (
                not provider.available
                or provider.stopped
                or (work.code == "run_budget_exhausted" and provider.max_requests_per_run <= await run_usage(sessions, work.id))
            )
        ):
            continue
        await queue.enqueue("lookup_remote_tracklist", work_id=str(work.id), key=f"remote-work:{work.id}:{work.updated_at.timestamp()}")


async def run_usage(sessions: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> int:

    async with sessions() as session:
        return int(await session.scalar(select(func.count()).where(RemoteTransportAttempt.run_id == run_id)) or 0)


async def batch_status(sessions: async_sessionmaker[AsyncSession], batch_id: uuid.UUID) -> dict[str, Any] | None:
    async with sessions() as session:
        batch = await session.get(RemoteLookupBatch, batch_id)
        if batch is None:
            return None
        counts = dict(
            (
                await session.execute(
                    select(RemoteLookupWork.status, func.count())
                    .join(RemoteLookupMembership, RemoteLookupMembership.work_id == RemoteLookupWork.id)
                    .where(RemoteLookupMembership.batch_id == batch_id)
                    .group_by(RemoteLookupWork.status)
                )
            ).all()
        )
        rows = list(
            await session.scalars(
                select(RemoteLookupWork)
                .join(RemoteLookupMembership, RemoteLookupMembership.work_id == RemoteLookupWork.id)
                .where(RemoteLookupMembership.batch_id == batch_id)
                .order_by(RemoteLookupWork.created_at, RemoteLookupWork.id)
                .limit(50)
            )
        )
        return {"batch": batch, "counts": counts, "work": rows}
