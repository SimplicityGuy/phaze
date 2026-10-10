"""Controller-only capture dispatch and report validation. All network work is session-free."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureCompanionPayload, CaptureReport, CaptureResponse, CaptureTarget
from phaze.services.provider_persistence import lock_source_bucket, source_availability, store_source_read
from phaze.tracklist_providers.domain import OutcomeStatus, SourceIdentity, SourceRead


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from phaze.services.agent_task_router import AgentTaskRouter


async def enqueue_companion_capture(
    session_factory: async_sessionmaker[AsyncSession],
    task_router: AgentTaskRouter,
    file_id: uuid.UUID,
    *,
    media_id: uuid.UUID | None = None,
    budget: CaptureBudget | None = None,
) -> CaptureCompanionPayload:
    """Resolve authorized inventory, close read transaction, then enqueue on the owning meta lane."""
    async with session_factory() as session:
        file = await session.get(FileRecord, file_id)
        if file is None or "." + file.file_type not in INGESTIBLE_COMPANION_EXTENSIONS:
            raise ValueError("Capture requires an inventoried companion")
        if media_id is not None:
            linked = await session.scalar(select(FileCompanion.id).where(FileCompanion.companion_id == file_id, FileCompanion.media_id == media_id))
            if linked is None:
                raise ValueError("Companion is not linked to the requested recording")
        payload = CaptureCompanionPayload(
            agent_id=file.agent_id,
            target=CaptureTarget(
                file_id=file.id, media_id=media_id, path=file.current_path, expected_sha256=file.sha256_hash, expected_size=file.file_size
            ),
            budget=budget or CaptureBudget(),
        )
    queue = task_router.queue_for(payload.agent_id, "meta")
    await queue.connect()
    await queue.enqueue("capture_companion_source", **payload.model_dump())
    return payload


async def store_capture_report(session: AsyncSession, agent_id: str, report: CaptureReport) -> CaptureResponse:
    """Recheck live ownership/path/revision/link and append read evidence; caller commits briefly."""
    target = report.target
    identity = SourceIdentity(provider_id="local", native_id=f"companion:{target.file_id}")
    owned = await session.scalar(select(FileRecord.id).where(FileRecord.id == target.file_id, FileRecord.agent_id == agent_id))
    if owned is None:
        raise PermissionError("Source is not an owned companion")
    # Serialize the provider bucket before inventory; object locks follow inventory so
    # deletion's file -> FK SET NULL object order cannot invert this callback.
    await lock_source_bucket(session, identity)
    file = await session.scalar(
        select(FileRecord)
        .where(FileRecord.id == target.file_id, FileRecord.agent_id == agent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if file is None or "." + file.file_type not in INGESTIBLE_COMPANION_EXTENSIONS:
        raise PermissionError("Source is not an owned companion")
    if target.media_id is not None:
        linked = await session.scalar(
            select(FileCompanion.id)
            .where(FileCompanion.companion_id == target.file_id, FileCompanion.media_id == target.media_id)
            .with_for_update(read=True)
        )
        if linked is None:
            raise ValueError("Source is no longer linked to the requested recording")
    stale = file.current_path != target.path or file.sha256_hash != target.expected_sha256 or file.file_size != target.expected_size
    read = report.read
    if stale:
        evidence = tuple(dict.fromkeys(("inventory_changed", *(entry for entry in read.evidence if entry.startswith("revision:")), *read.evidence)))[
            :64
        ]
        read = SourceRead.model_validate(
            read.model_dump(mode="json") | {"status": OutcomeStatus.RETRY, "code": "inventory_changed", "evidence": evidence}
        )
    stored = await store_source_read(
        session,
        identity,
        read,
        parser_version="source-read-v1",
        source_file_id=target.file_id,
        channel="companion",
    )
    freshness = "stale" if stale else await source_availability(session, stored.observation, target.media_id or target.file_id)
    # With no target recording, association freshness is not asserted; the source remains readable.
    if target.media_id is None and freshness == "unlinked":
        freshness = read.status.value
    return CaptureResponse(observation_id=stored.observation.id, reused=stored.reused, freshness=freshness)
