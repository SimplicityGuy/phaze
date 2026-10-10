"""Controller-only capture dispatch and report validation. All network work is session-free."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from sqlalchemy import select, text

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS, is_quarantined
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureCompanionPayload, CaptureReport, CaptureResponse, CaptureTarget
from phaze.services.companion_acquisition import record_capture_attempt
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
    request_key: str | None = None,
    expected_agent_id: str | None = None,
) -> CaptureCompanionPayload:
    """Resolve authorized inventory, close read transaction, then enqueue on the owning meta lane."""
    async with session_factory() as session:
        file = await session.get(FileRecord, file_id)
        if file is None or "." + file.file_type not in INGESTIBLE_COMPANION_EXTENSIONS:
            raise ValueError("Capture requires an inventoried companion")
        if expected_agent_id is not None and file.agent_id != expected_agent_id:
            raise ValueError("Companion owner changed before dispatch")
        await require_capture_eligible(session, file)
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
    if request_key is not None and (len(request_key) > 128 or "\0" in request_key):
        raise ValueError("Capture request key must be bounded")
    options = {"key": request_key} if request_key is not None else {}
    await queue.enqueue("capture_companion_source", **payload.model_dump(), **options)
    return payload


async def store_capture_report(session: AsyncSession, agent_id: str, report: CaptureReport) -> CaptureResponse:
    """Recheck live ownership/path/revision/link and append read evidence; caller commits briefly."""
    target = report.target
    if report.attempt_id is not None:
        key = int.from_bytes(hashlib.sha256(f"capture-attempt:{report.attempt_id}".encode()).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
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
    read = SourceRead.model_validate(
        report.read.model_dump(mode="json")
        | {
            "evidence": tuple(dict.fromkeys((f"capture:inventory_sha256:{target.expected_sha256}", *report.read.evidence)))[:64],
        }
    )
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
    await record_capture_attempt(session, agent_id, report, stored, freshness)
    return CaptureResponse(observation_id=stored.observation.id, reused=stored.reused, freshness=freshness)


async def require_capture_eligible(session: AsyncSession, file: FileRecord) -> None:
    """Recheck current inventory and junk immediately before any capture dispatch."""
    if file.missing_at is not None or file.companion_ambiguous_at is not None:
        raise ValueError("Companion unavailable or ambiguous")
    if is_quarantined(file.original_path) or is_quarantined(file.current_path):
        raise ValueError("Quarantined companion cannot be read")
    features = await session.get(CompanionContentFeatures, file.id)
    if features is not None and features.fingerprint == file.sha256_hash and features.junk_class is not None:
        raise ValueError("Current junk companion cannot be read")
