"""Durable, bounded per-agent companion capture/import orchestration. No selected writes."""

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
from typing import Any
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS, is_quarantined
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_import import CompanionImportItem, CompanionImportRun, ProviderAcquisitionAttempt
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import ProviderSourceObservation
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.companion_capture import enqueue_companion_capture, require_capture_eligible
from phaze.services.local_source_import import import_local_source
from phaze.services.provider_persistence import find_source_object_id, lock_source_bucket
from phaze.tracklist_providers.domain import SourceIdentity
from phaze.tracklist_providers.local_parser import PARSER_VERSION
from phaze.tracklist_providers.local_release import RELEASE_PARSER_VERSION


PAGE_MAX = 50
ACTIVE_STATES = ("pending", "awaiting_capture", "importing")
OUTCOMES = (
    "current",
    "pending",
    "stale",
    "parser_old",
    "missing",
    "ambiguous",
    "unavailable",
    "unsupported",
    "incomplete",
    "failed",
    "quarantined",
    "junk",
    "unresolved",
)


def _page_size(size: int) -> int:
    if not 1 <= size <= PAGE_MAX:
        raise ValueError("Page size must be between 1 and 50")
    return size


async def classify_source(session: AsyncSession, file: FileRecord) -> tuple[str, str]:
    """Narrow source status; observation/content chronology is not acquisition chronology."""
    if is_quarantined(file.original_path) or is_quarantined(file.current_path):
        return "quarantined", "quarantined"
    if file.missing_at is not None:
        return "missing", "inventory_missing"
    if file.companion_ambiguous_at is not None:
        return "ambiguous", "inventory_ambiguous"
    features = await session.get(CompanionContentFeatures, file.id)
    if features is not None and features.fingerprint == file.sha256_hash and features.junk_class:
        return "junk", "current_junk"
    if features is None or features.fingerprint != file.sha256_hash:
        return "pending", "awaiting_features"
    source = await find_source_object_id(session, SourceIdentity(provider_id="local", native_id=f"companion:{file.id}"))
    if source is None:
        return "pending", "needs_capture"
    attempt = (
        await session.execute(
            select(
                ProviderAcquisitionAttempt.status,
                ProviderAcquisitionAttempt.freshness,
                ProviderAcquisitionAttempt.envelope.op("->")("target").op("->>")("expected_sha256").label("expected_sha256"),
                ProviderAcquisitionAttempt.observation_id,
            )
            .where(ProviderAcquisitionAttempt.source_object_id == source)
            .order_by(ProviderAcquisitionAttempt.ordinal.desc())
            .limit(1)
        )
    ).first()
    if attempt is not None:
        if attempt.expected_sha256 != file.sha256_hash or attempt.freshness == "stale":
            return "stale", "inventory_changed"
        if attempt.status not in {"found", "incomplete"}:
            return {"absent": "missing", "unsupported": "unsupported", "retry": "failed", "contract_error": "failed"}.get(
                attempt.status, "unavailable"
            ), "latest_capture_attempt"
    raw_id = attempt.observation_id if attempt is not None else await _retained_read_id(session, file)
    if raw_id is None:
        return "parser_old", "needs_current_parser"
    parsers = (
        await session.execute(
            select(
                ProviderSourceObservation.parser_version,
                func.bool_or(ProviderSourceObservation.status == "found").label("has_complete"),
                func.bool_or(ProviderSourceObservation.status == "unsupported").label("has_unsupported"),
            )
            .where(
                ProviderSourceObservation.object_id == source,
                ProviderSourceObservation.parent_id == raw_id,
                ProviderSourceObservation.parser_version.in_([PARSER_VERSION, RELEASE_PARSER_VERSION]),
            )
            .group_by(ProviderSourceObservation.parser_version)
        )
    ).all()
    # Interpretations inherit the raw acquisition timestamp, so neither that
    # timestamp nor a random observation UUID establishes import chronology.
    # A retained complete interpretation of this EXACT current raw parent and
    # parser satisfies backfill; this classification never selects authority.
    statuses = {row.parser_version: "found" if row.has_complete else "unsupported" if row.has_unsupported else "incomplete" for row in parsers}
    track_status = statuses.get(PARSER_VERSION, "unsupported" if file.file_type not in {"txt", "nfo", "cue"} else "pending")
    release_status = statuses.get(RELEASE_PARSER_VERSION, "pending")
    if track_status == "pending" or release_status == "pending":
        return "parser_old", "needs_current_parser"
    if track_status == "unsupported":
        return "unsupported", "tracklist:unsupported"
    if track_status != "found" or release_status != "found":
        return "incomplete", f"tracklist:{track_status};release:{release_status}"
    return "current", "parsed_current"


async def count_backfill(session: AsyncSession, *, agent_id: str | None = None, page_size: int = PAGE_MAX) -> dict[str, int]:
    """Read-only keyset count over all sources; never queues, opens files or mutates links."""
    _page_size(page_size)
    counts = dict.fromkeys(OUTCOMES, 0)
    cutoff = await session.scalar(select(func.clock_timestamp()))
    after = None
    while True:
        query = select(FileRecord).where(
            FileRecord.file_type.in_([ext[1:] for ext in INGESTIBLE_COMPANION_EXTENSIONS]), FileRecord.created_at <= cutoff
        )
        if agent_id is not None:
            query = query.where(FileRecord.agent_id == agent_id)
        if after is not None:
            query = query.where(FileRecord.id > after)
        rows = (await session.scalars(query.order_by(FileRecord.id).limit(page_size))).all()
        if not rows:
            break
        for file in rows:
            state, _code = await classify_source(session, file)
            counts[state] += 1
        after = rows[-1].id
    return counts


async def create_run(session: AsyncSession, agent_id: str, *, associated: bool = False, request_key: str | None = None) -> CompanionImportRun:
    """Caller has just committed fresh association, or schedules it before this invocation."""
    if request_key is not None:
        if len(request_key) > 128:
            raise ValueError("Run request key exceeds bound")
        lock = int.from_bytes(hashlib.sha256(f"companion-import:{request_key}".encode()).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        previous = await session.scalar(select(CompanionImportRun).where(CompanionImportRun.request_key == request_key))
        if previous is not None:
            if previous.agent_id != agent_id:
                raise ValueError("Run request reused for another owner")
            return previous
    row = CompanionImportRun(agent_id=agent_id, associated=associated, request_key=request_key)
    session.add(row)
    await session.flush()
    return row


async def enumerate_page(session: AsyncSession, run_id: uuid.UUID, *, page_size: int = PAGE_MAX) -> int:
    """Checkpoint cursor and work rows in the SAME transaction; caller commits one page."""
    _page_size(page_size)
    run = await session.scalar(select(CompanionImportRun).where(CompanionImportRun.id == run_id).with_for_update())
    if run is None:
        raise ValueError("Unknown companion import run")
    if run.enumerated:
        return 0
    query = select(FileRecord).where(
        FileRecord.agent_id == run.agent_id,
        FileRecord.created_at <= run.cutoff,
        FileRecord.file_type.in_([ext[1:] for ext in INGESTIBLE_COMPANION_EXTENSIONS]),
    )
    if run.cursor is not None:
        query = query.where(FileRecord.id > run.cursor)
    rows = (await session.scalars(query.order_by(FileRecord.id).limit(page_size))).all()
    for file in rows:
        state, code = await classify_source(session, file)
        terminal = code in {"quarantined", "current_junk", "inventory_missing", "inventory_ambiguous"}
        session.add(
            CompanionImportItem(run_id=run.id, file_id=file.id, expected_sha256=file.sha256_hash, state=state if terminal else "pending", code=code)
        )
    if rows:
        run.cursor = rows[-1].id
    run.enumerated = len(rows) < page_size
    await session.flush()
    return len(rows)


async def run_status(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    """Aggregate counts, no ever-growing in-memory work/history list."""
    run = await session.get(CompanionImportRun, run_id)
    if run is None:
        raise ValueError("Unknown companion import run")
    counts = dict(
        (
            await session.execute(
                select(CompanionImportItem.state, func.count()).where(CompanionImportItem.run_id == run_id).group_by(CompanionImportItem.state)
            )
        ).all()
    )
    return {
        "run_id": str(run.id),
        "agent_id": run.agent_id,
        "enumerated": run.enumerated,
        "continuation": str(run.continuation),
        "counts": counts,
        "complete": run.enumerated and not any(counts.get(state, 0) for state in ACTIVE_STATES),
    }


async def list_run_items(session: AsyncSession, run_id: uuid.UUID, *, after: uuid.UUID | None = None, limit: int = PAGE_MAX) -> list[dict[str, Any]]:
    """Explicit bounded status page; retained content is fetched only by detail readers."""
    _page_size(limit)
    query = select(
        CompanionImportItem.id,
        CompanionImportItem.file_id,
        CompanionImportItem.state,
        CompanionImportItem.code,
        CompanionImportItem.attempts,
        CompanionImportItem.tracklist_status,
        CompanionImportItem.release_status,
    ).where(CompanionImportItem.run_id == run_id)
    if after is not None:
        query = query.where(CompanionImportItem.id > after)
    return [dict(row) for row in (await session.execute(query.order_by(CompanionImportItem.id).limit(limit))).mappings()]


async def _retained_read_id(session: AsyncSession, file: FileRecord, since: datetime | None = None) -> uuid.UUID | None:
    source_id = await find_source_object_id(session, SourceIdentity(provider_id="local", native_id=f"companion:{file.id}"))
    if source_id is None:
        return None
    query = select(ProviderAcquisitionAttempt.observation_id).where(
        ProviderAcquisitionAttempt.source_object_id == source_id,
        ProviderAcquisitionAttempt.envelope.op("->")("target").op("->>")("expected_sha256") == file.sha256_hash,
    )
    if since is not None:
        query = query.where(ProviderAcquisitionAttempt.received_at >= since)
    observation_id = await session.scalar(query.order_by(ProviderAcquisitionAttempt.ordinal.desc()).limit(1))
    if observation_id is not None:
        return observation_id
    if since is not None:
        return None
    return await session.scalar(
        select(ProviderSourceObservation.id)
        .where(
            ProviderSourceObservation.object_id == source_id,
            ProviderSourceObservation.parser_version == "source-read-v1",
            ProviderSourceObservation.payload.op("->")("evidence").op("?")(f"capture:inventory_sha256:{file.sha256_hash}"),
        )
        .order_by(ProviderSourceObservation.retrieved_at.desc(), ProviderSourceObservation.id.desc())
        .limit(1)
    )


async def _retained_read(session: AsyncSession, file: FileRecord, since: datetime | None = None) -> ProviderSourceObservation | None:
    observation_id = await _retained_read_id(session, file, since)
    return await session.get(ProviderSourceObservation, observation_id) if observation_id is not None else None


async def _claim_item(session: AsyncSession, item_id: uuid.UUID, now: datetime) -> tuple[CompanionImportItem, uuid.UUID] | None:
    item = await session.scalar(
        select(CompanionImportItem).where(CompanionImportItem.id == item_id).with_for_update().execution_options(populate_existing=True)
    )
    if item is None or item.state not in ACTIVE_STATES or (item.lease_until is not None and item.lease_until > now):
        return None
    token = uuid.uuid4()
    item.lease_token, item.lease_until = token, now + timedelta(seconds=30)
    item.attempts += 1
    return item, token


async def process_item(
    factory: async_sessionmaker[AsyncSession], router: AgentTaskRouter, item_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """One bounded source/target step. No DB session stays open across queue/network I/O."""
    now = now or datetime.now(UTC)
    async with factory.begin() as session:
        claimed = await _claim_item(session, item_id, now)
        if claimed is None:
            return
        item, token = claimed
        run = await session.get(CompanionImportRun, item.run_id)
        if run is None:
            raise ValueError("Import item has no owning run")
        owner = run.agent_id
        file = await session.get(FileRecord, item.file_id)
        if file is None:
            item.state, item.code = "missing", "deleted"
            return
        if file.agent_id != owner:
            item.state, item.code = "stale", "owner_changed"
            return
        state, code = await classify_source(session, file)
        if code in {"quarantined", "current_junk", "inventory_missing", "inventory_ambiguous"}:
            item.state, item.code = state, code
            return
        if file.sha256_hash != item.expected_sha256:
            item.state, item.code = "stale", "inventory_changed"
            return
        if code == "awaiting_features":
            item.state, item.code = "unavailable", code
            return
        await require_capture_eligible(session, file)
        observation = await _retained_read(session, file, item.dispatched_at if item.state == "awaiting_capture" else None)
        if observation is not None and item.state == "pending" and observation.status not in {"found", "incomplete"}:
            observation = None
        if observation is not None:
            if observation.status not in {"found", "incomplete"}:
                item.state = {"absent": "missing", "unsupported": "unsupported", "retry": "failed", "contract_error": "failed"}.get(
                    observation.status, "unavailable"
                )
                item.code = "capture_outcome"
                return
            item.observation_id, item.state = observation.id, "importing"
            target = select(FileCompanion.media_id).where(
                FileCompanion.companion_id == file.id, FileCompanion.derivation_revision == file.sha256_hash, FileCompanion.derived_at.is_not(None)
            )
            if item.target_cursor is not None:
                target = target.where(FileCompanion.media_id > item.target_cursor)
            media_id = await session.scalar(target.order_by(FileCompanion.media_id).limit(1))
            if media_id is None:
                item.state = (
                    "unresolved"
                    if item.target_cursor is None or item.had_unresolved
                    else (
                        "unsupported"
                        if "unsupported" in (item.tracklist_status, item.release_status)
                        else "incomplete"
                        if item.had_incomplete
                        else "current"
                    )
                )
                item.code = "no_fresh_target" if item.target_cursor is None else "imported"
                item.lease_until = None
                return
            # Release item lock BEFORE source-bucket/inventory locks. The fenced final
            # checkpoint occurs in another transaction after exactly one target import.
            raw_id = observation.id
        elif item.state == "awaiting_capture":
            server_now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if item.dispatched_at is not None and server_now - item.dispatched_at >= timedelta(minutes=15):
                item.state, item.code, item.lease_until = "unavailable", "capture_timeout", None
                return
            if item.last_enqueued_at is not None and now - item.last_enqueued_at < timedelta(seconds=30):
                item.lease_until = None
                return
            media_id, raw_id = None, None
            item.last_enqueued_at = now
        else:
            media_id, raw_id = None, None
            # Receipt timestamps come from the database clock, so the intent
            # baseline must use that same clock rather than controller wall time.
            item.state, item.dispatched_at, item.code = "awaiting_capture", await session.scalar(select(func.clock_timestamp())), "capture_requested"
            item.last_enqueued_at = now
        file_id = file.id
    if raw_id is not None and media_id is not None:
        try:
            async with factory.begin() as session:
                await lock_source_bucket(session, SourceIdentity(provider_id="local", native_id=f"companion:{file_id}"))
                await session.execute(
                    select(FileRecord.id)
                    .where(FileRecord.id.in_([file_id, media_id]))
                    .order_by(FileRecord.original_path, FileRecord.id)
                    .with_for_update()
                )
                locked_source = await session.get(FileRecord, file_id, populate_existing=True)
                if locked_source is None or locked_source.agent_id != owner:
                    raise ValueError("Source owner changed before import")
                link = await session.scalar(
                    select(FileCompanion.id)
                    .where(
                        FileCompanion.companion_id == file_id,
                        FileCompanion.media_id == media_id,
                        FileCompanion.derivation_revision == file.sha256_hash,
                        FileCompanion.derived_at.is_not(None),
                    )
                    .with_for_update()
                )
                if link is None:
                    raise ValueError("Association evidence changed before import")
                result = await import_local_source(session, ImportLocalSource(media_id=media_id, raw_observation_id=raw_id))
            outcome = "parsed_incomplete" if result.tracklist_status != "found" or result.release_status != "found" else "parsed_current"
            error = None
        except (ValueError, PermissionError) as exc:
            outcome, error = "target_changed", str(exc)
        async with factory.begin() as session:
            final_item = await session.get(CompanionImportItem, item_id, with_for_update=True)
            if final_item is not None and final_item.lease_token == token:
                final_item.target_cursor = media_id
                final_item.code = outcome
                final_item.lease_until = None
                final_item.had_incomplete = final_item.had_incomplete or outcome == "parsed_incomplete"
                final_item.had_unresolved = final_item.had_unresolved or error is not None
                if error is None:
                    final_item.tracklist_status, final_item.release_status = result.tracklist_status, result.release_status
        return
    try:
        async with asyncio.timeout(10):
            await enqueue_companion_capture(factory, router, file_id, request_key=f"capture-backfill:{item_id}", expected_agent_id=owner)
        code = "capture_queued"
    except Exception:
        # Callback may already exist, so await/reconcile it even if the enqueue raised.
        code = "enqueue_unconfirmed"
    async with factory.begin() as session:
        final_item = await session.get(CompanionImportItem, item_id, with_for_update=True)
        if final_item is not None and final_item.lease_token == token:
            final_item.code, final_item.lease_until = code, None


async def process_run_page(
    factory: async_sessionmaker[AsyncSession], router: AgentTaskRouter, run_id: uuid.UUID, *, page_size: int = PAGE_MAX
) -> dict[str, Any]:
    """One durable enumeration page and at most fifty resumable item steps."""
    _page_size(page_size)
    async with factory.begin() as session:
        await enumerate_page(session, run_id, page_size=page_size)
        ids = list(
            await session.scalars(
                select(CompanionImportItem.id)
                .where(CompanionImportItem.run_id == run_id, CompanionImportItem.state.in_(ACTIVE_STATES))
                .order_by(CompanionImportItem.attempts, CompanionImportItem.id)
                .limit(page_size)
            )
        )
    for item_id in ids:
        await process_item(factory, router, item_id)
    async with factory() as session:
        return await run_status(session, run_id)
