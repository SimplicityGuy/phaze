"""Synthetic durable import/read-attempt seams, with no production archive access."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_import import CompanionImportItem, CompanionImportRun, ProviderAcquisitionAttempt
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.provider_source import ProviderRecordingSelection, ProviderSourceObject, ProviderSourceObservation
from phaze.schemas.agent_companion_capture import CaptureReport, CaptureTarget
from phaze.services.companion_acquisition import get_latest_source_attempts
from phaze.services.companion_capture import enqueue_companion_capture, store_capture_report
from phaze.services.companion_import_backfill import count_backfill, create_run, enumerate_page, list_run_items, process_item, run_status
from phaze.tracklist_providers.domain import SourceRead
from tests.db_guard import BLOCKED_WAITER_SQL
from tests.integration.test_local_source_import import TEXT, inventory


async def features(session, companion):
    session.add(
        CompanionContentFeatures(
            file_id=companion.id,
            agent_id=companion.agent_id,
            fingerprint=companion.sha256_hash,
            encoding="utf-8",
            byte_size=companion.file_size,
            media_references=[],
            reference_count=0,
            is_tracklist=True,
            folder_media=[],
            folder_media_count=0,
            extractor_version=1,
        )
    )
    link = await session.scalar(select(FileCompanion).where(FileCompanion.companion_id == companion.id))
    if link is not None:
        link.derivation_method = "stem"
        link.derivation_revision = companion.sha256_hash
        link.derivation_evidence = ["synthetic:fresh"]
        link.derived_at = datetime.now(UTC)
    await session.flush()


def report(companion, *, status="found", identifier=None, at=None):
    read = SourceRead(
        status=status,
        code="read" if status == "found" else "offline",
        scope=str(companion.id),
        retrieved_at=at or datetime.now(UTC),
        **(
            {
                "text": TEXT,
                "encoding": "utf-8",
                "revision": companion.sha256_hash,
                "revision_scope": "full",
                "bytes_read": companion.file_size,
                "evidence": ("revision:sha256:full",),
            }
            if status == "found"
            else {}
        ),
    )
    return CaptureReport(
        attempt_id=identifier or uuid.uuid4(),
        target=CaptureTarget(
            file_id=companion.id, path=companion.current_path, expected_sha256=companion.sha256_hash, expected_size=companion.file_size
        ),
        read=read,
    )


async def test_attempts_are_received_order_not_semantic_observation_order(session):
    _media, companion, _raw = await inventory(session)
    first = report(companion, status="unavailable")
    a = await store_capture_report(session, companion.agent_id, first)
    b = await store_capture_report(session, companion.agent_id, report(companion))
    last = report(companion, status="unavailable")
    c = await store_capture_report(session, companion.agent_id, last)
    assert c.observation_id == a.observation_id != b.observation_id
    await store_capture_report(session, companion.agent_id, last)
    assert await session.scalar(select(func.count()).select_from(ProviderAcquisitionAttempt)) == 3
    object_id = await session.scalar(select(ProviderSourceObject.id).where(ProviderSourceObject.source_file_id == companion.id))
    latest = (await get_latest_source_attempts(session, [object_id]))[object_id]
    assert latest.status == "unavailable" and latest.ordinal == 3 and latest.attempt_id == last.attempt_id
    assert latest.observation_id == a.observation_id
    assert await get_latest_source_attempts(session, []) == {}
    with pytest.raises(ValueError, match="at most 50"):
        await get_latest_source_attempts(session, [object_id] * 51)
    with pytest.raises(ValueError, match="different report"):
        await store_capture_report(session, companion.agent_id, report(companion, identifier=first.attempt_id))


async def test_report_retry_after_inventory_change_keeps_one_attempt_and_rolls_back(session):
    _media, companion, _raw = await inventory(session)
    sent = report(companion)
    await store_capture_report(session, companion.agent_id, sent)
    before = await session.scalar(select(func.count()).select_from(ProviderSourceObservation))
    companion.sha256_hash = "b" * 64
    await session.flush()
    with pytest.raises(ValueError, match="different report"):
        async with session.begin_nested():
            await store_capture_report(session, companion.agent_id, sent)
    assert await session.scalar(select(func.count()).select_from(ProviderAcquisitionAttempt)) == 1
    assert await session.scalar(select(func.count()).select_from(ProviderSourceObservation)) == before


async def test_attempt_received_clock_is_after_baseline_in_an_already_open_transaction(session):
    _media, companion, _raw = await inventory(session)
    started = await session.scalar(select(func.now()))
    baseline = await session.scalar(select(func.clock_timestamp()))
    body = report(companion)
    await store_capture_report(session, companion.agent_id, body)
    received = await session.get(ProviderAcquisitionAttempt, body.attempt_id)
    assert received.received_at >= baseline > started


async def test_concurrent_attempt_uuid_cannot_cross_source_intents(async_engine, tmp_path):
    """Separate bucket locks must still serialize one globally unique physical-read identity."""
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    companions = [
        FileRecord(
            id=uuid.uuid4(),
            agent_id="test-fileserver",
            file_type="txt",
            original_filename=f"notes-{index}.txt",
            original_path=str(tmp_path / f"notes-{index}.txt"),
            current_path=str(tmp_path / f"notes-{index}.txt"),
            sha256_hash="a" * 64,
            file_size=0,
        )
        for index in range(2)
    ]
    attempt_id = uuid.uuid4()
    waiter = None
    backend = asyncio.get_running_loop().create_future()
    try:
        async with factory.begin() as db:
            db.add_all(companions)

        async def submit_other_intent():
            try:
                async with factory.begin() as db:
                    backend.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                    await store_capture_report(db, companions[1].agent_id, report(companions[1], status="unavailable", identifier=attempt_id))
                return "stored"
            except ValueError as error:
                assert "different report" in str(error)
                return "rejected"

        # Keep the first authenticated report transaction open after it owns the
        # attempt-UUID lock. The other source has a distinct bucket, so only that
        # global attempt lock can protect this cross-source intent collision.
        async with factory.begin() as holding:
            await store_capture_report(holding, companions[0].agent_id, report(companions[0], status="unavailable", identifier=attempt_id))
            waiter = asyncio.create_task(submit_other_intent())
            waiter_pid = await asyncio.wait_for(backend, timeout=5)
            scoped_waiter = text(BLOCKED_WAITER_SQL.removesuffix(")") + " AND a.pid = :waiter_pid AND l.locktype = 'advisory')")
            async with asyncio.timeout(5):
                while not await holding.scalar(scoped_waiter, {"waiter_pid": waiter_pid}):
                    assert not waiter.done(), "second source intent must queue behind the held attempt lock"
            assert not waiter.done()
        assert await asyncio.wait_for(waiter, timeout=5) == "rejected"
        async with factory() as db:
            attempt = await db.get(ProviderAcquisitionAttempt, attempt_id)
            assert attempt is not None and attempt.ordinal == 1
            source = await db.get(ProviderSourceObject, attempt.source_object_id)
            assert source.native_id == f"companion:{companions[0].id}"
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        async with factory.begin() as db:
            source_ids = select(ProviderSourceObject.id).where(ProviderSourceObject.source_file_id.in_([row.id for row in companions]))
            await db.execute(delete(ProviderAcquisitionAttempt).where(ProviderAcquisitionAttempt.source_object_id.in_(source_ids)))
            await db.execute(delete(ProviderSourceObservation).where(ProviderSourceObservation.object_id.in_(source_ids)))
            await db.execute(delete(ProviderSourceObject).where(ProviderSourceObject.source_file_id.in_([row.id for row in companions])))
            await db.execute(delete(FileRecord).where(FileRecord.id.in_([row.id for row in companions])))


async def test_atomic_keyset_pages_fixed_cutoff_and_readonly_counts(session):
    companions = []
    for index in range(7):
        _media, companion, _raw = await inventory(session)
        _media.original_path = _media.current_path = f"/synthetic/media-{index}.mp3"
        companion.original_path = companion.current_path = f"/synthetic/notes-{index}.txt"
        await session.flush()
        await features(session, companion)
        companions.append(companion)
    run = await create_run(session, "test-fileserver", associated=True)
    late = FileRecord(
        id=uuid.UUID(int=1),
        agent_id="test-fileserver",
        original_path="/synthetic/late.txt",
        current_path="/synthetic/late.txt",
        original_filename="late.txt",
        file_type="txt",
        sha256_hash="c" * 64,
        file_size=0,
        created_at=run.cutoff + timedelta(seconds=1),
    )
    session.add(late)
    await session.flush()
    with pytest.raises(RuntimeError):
        async with session.begin_nested():
            await enumerate_page(session, run.id, page_size=2)
            raise RuntimeError("crash before checkpoint commit")
    assert await session.scalar(select(func.count()).select_from(CompanionImportItem)) == 0
    await session.refresh(run)
    assert run.cursor is None
    while not run.enumerated:
        await enumerate_page(session, run.id, page_size=2)
    assert await session.scalar(select(func.count()).select_from(CompanionImportItem)) == 7
    assert late.id not in await session.scalars(select(CompanionImportItem.file_id))
    status = await run_status(session, run.id)
    assert status["counts"] == {"pending": 7}
    page = await list_run_items(session, run.id, limit=2)
    assert len(page) == 2
    assert len(await list_run_items(session, run.id, after=page[-1]["id"], limit=2)) == 2
    with pytest.raises(ValueError, match="Page size"):
        await enumerate_page(session, run.id, page_size=51)
    before = await session.scalar(select(func.count()).select_from(CompanionImportRun))
    counts = await count_backfill(session, page_size=2)
    assert sum(counts.values()) == 7
    assert await session.scalar(select(func.count()).select_from(CompanionImportRun)) == before


def maker(session):
    return async_sessionmaker(session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")


async def test_stored_capture_import_resumes_targets_and_never_selects(session):
    media, companion, _raw = await inventory(session)
    await features(session, companion)
    run = await create_run(session, media.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item_id = await session.scalar(select(CompanionImportItem.id))
    await session.commit()
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    router = SimpleNamespace(queue_for=lambda *_: queue)
    for _ in range(3):
        await process_item(maker(session), router, item_id)
    async with maker(session)() as db:
        item = await db.get(CompanionImportItem, item_id)
        assert item.state == "current" and item.tracklist_status == "found" and item.release_status == "found"
        assert await db.scalar(select(func.count()).select_from(ProviderRecordingSelection)) == 0
        assert await db.scalar(select(func.count()).select_from(ProviderSourceObservation)) >= 3
    queue.enqueue.assert_not_awaited()


async def test_old_failure_recaptures_queue_error_resumes_and_quarantine_race(session):
    _media, companion, _raw = await inventory(session)
    await features(session, companion)
    await store_capture_report(session, companion.agent_id, report(companion, status="unavailable"))
    run = await create_run(session, companion.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item_id = await session.scalar(select(CompanionImportItem.id))
    await session.commit()
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock(side_effect=RuntimeError("lost ack")))
    router = SimpleNamespace(queue_for=lambda *_: queue)
    now = datetime.now(UTC)
    await process_item(maker(session), router, item_id, now=now)
    assert queue.enqueue.await_count == 1
    await process_item(maker(session), router, item_id, now=now + timedelta(seconds=1))
    assert queue.enqueue.await_count == 1
    queue.enqueue.side_effect = None
    await process_item(maker(session), router, item_id, now=now + timedelta(seconds=31))
    async with maker(session)() as db:
        checkpoint = await db.get(CompanionImportItem, item_id)
        diagnostic = (checkpoint.state, checkpoint.code, checkpoint.last_enqueued_at, checkpoint.lease_until, checkpoint.dispatched_at)
    assert queue.enqueue.await_count == 2, diagnostic
    assert queue.enqueue.await_args.kwargs["key"] == f"capture-backfill:{item_id}"
    async with maker(session).begin() as db:
        file = await db.get(FileRecord, companion.id)
        file.current_path = "/synthetic/.phaze-quarantine/notes.txt"
    await process_item(maker(session), router, item_id, now=now + timedelta(seconds=32))
    async with maker(session)() as db:
        assert (await db.get(CompanionImportItem, item_id)).state == "quarantined"
    with pytest.raises(ValueError, match="Quarantined"):
        await enqueue_companion_capture(maker(session), router, companion.id)
    assert queue.enqueue.await_count == 2


async def test_dispatch_baseline_uses_server_clock_and_accepts_new_attempt_reusing_old_failure(session):
    _media, companion, _raw = await inventory(session)
    await features(session, companion)
    from phaze.services.companion_capture import store_capture_report

    old = await store_capture_report(session, companion.agent_id, report(companion, status="unavailable"))
    run = await create_run(session, companion.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item_id = await session.scalar(select(CompanionImportItem.id))
    await session.commit()
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    router = SimpleNamespace(queue_for=lambda *_: queue)
    # Deliberately skew controller time ahead of the DB clock. The held outer
    # transaction started even earlier; neither is a truthful receipt baseline.
    client_now = datetime.now(UTC) + timedelta(hours=1)
    await process_item(maker(session), router, item_id, now=client_now)
    await process_item(maker(session), router, item_id, now=client_now + timedelta(seconds=1))
    async with maker(session).begin() as db:
        item = await db.get(CompanionImportItem, item_id)
        assert item.state == "awaiting_capture" and item.code not in {"capture_outcome", "capture_timeout"}
        old_attempt = await db.scalar(select(ProviderAcquisitionAttempt).order_by(ProviderAcquisitionAttempt.ordinal.desc()).limit(1))
        assert old_attempt.received_at < item.dispatched_at
        again = await store_capture_report(db, companion.agent_id, report(companion, status="unavailable"))
        assert again.observation_id == old.observation_id
    await process_item(maker(session), router, item_id, now=client_now + timedelta(seconds=2))
    async with maker(session)() as db:
        item = await db.get(CompanionImportItem, item_id)
        assert item.state == "unavailable" and item.code == "capture_outcome"
    queue.enqueue.assert_awaited_once()


async def test_owner_change_refuses_cross_owner_dispatch_and_late_features_retry(session):
    from phaze.models.agent import Agent

    media, companion, _raw = await inventory(session)
    session.add(Agent(id="second-synthetic", name="second-synthetic", kind="fileserver", scan_roots=[]))
    await session.flush()
    run = await create_run(session, media.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item_id = await session.scalar(select(CompanionImportItem.id))
    await session.commit()
    router = SimpleNamespace(queue_for=AsyncMock())
    await process_item(maker(session), router, item_id)
    async with maker(session).begin() as db:
        assert (await db.get(CompanionImportItem, item_id)).code == "awaiting_features"
        await features(db, await db.get(FileRecord, companion.id))
        retry = await create_run(db, media.agent_id, associated=True)
        await enumerate_page(db, retry.id)
        new_id = await db.scalar(select(CompanionImportItem.id).where(CompanionImportItem.run_id == retry.id))
        file = await db.get(FileRecord, companion.id)
        file.agent_id = "second-synthetic"
    await process_item(maker(session), router, new_id)
    async with maker(session)() as db:
        changed = await db.get(CompanionImportItem, new_id)
        assert changed.state == "stale" and changed.code == "owner_changed"
    router.queue_for.assert_not_called()


async def test_absent_read_recovers_in_new_run_but_missing_inventory_never_dispatches(session):
    from phaze.services.companion_import_backfill import classify_source

    _media, companion, _raw = await inventory(session)
    await features(session, companion)
    await store_capture_report(session, companion.agent_id, report(companion, status="absent"))
    assert await classify_source(session, companion) == ("missing", "latest_capture_attempt")
    # A prior physical read can miss the old path while current inventory is
    # present and eligible. The visible read outcome is not an inventory ban.
    companion.current_path = "/synthetic/recovered-notes.txt"
    assert companion.missing_at is None
    recovered = await create_run(session, companion.agent_id, associated=True)
    await enumerate_page(session, recovered.id)
    item = await session.scalar(select(CompanionImportItem).where(CompanionImportItem.run_id == recovered.id))
    assert item.state == "pending" and item.code == "latest_capture_attempt"
    item_id = item.id
    await session.commit()
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    router = SimpleNamespace(queue_for=lambda *_: queue)
    await process_item(maker(session), router, item_id)
    queue.enqueue.assert_awaited_once()
    async with maker(session).begin() as db:
        checkpoint = await db.get(CompanionImportItem, item_id)
        assert checkpoint.state == "awaiting_capture" and checkpoint.code == "capture_queued"
        missing = await db.get(FileRecord, companion.id)
        missing.missing_at = datetime.now(UTC)
        excluded = await create_run(db, companion.agent_id, associated=True)
        await enumerate_page(db, excluded.id)
        blocked = await db.scalar(select(CompanionImportItem).where(CompanionImportItem.run_id == excluded.id))
        assert blocked.state == "missing" and blocked.code == "inventory_missing"
        blocked_id = blocked.id
    await process_item(maker(session), router, blocked_id)
    assert queue.enqueue.await_count == 1


async def test_one_relinked_target_does_not_block_other_targets(session, monkeypatch):
    from phaze.services import companion_import_backfill as service

    media, companion, _raw = await inventory(session)
    await features(session, companion)
    second = FileRecord(
        id=uuid.uuid4(),
        agent_id=media.agent_id,
        original_path="/synthetic/second.mp3",
        current_path="/synthetic/second.mp3",
        original_filename="second.mp3",
        file_type="mp3",
        sha256_hash="d" * 64,
        file_size=38,
    )
    session.add(second)
    await session.flush()
    session.add(
        FileCompanion(
            companion_id=companion.id,
            media_id=second.id,
            derivation_method="reference",
            derivation_revision=companion.sha256_hash,
            derived_at=datetime.now(UTC),
        )
    )
    await session.flush()
    ids = sorted([media.id, second.id])
    actual = service.import_local_source
    calls = []

    async def changed(db, command):
        calls.append(command.media_id)
        if command.media_id == ids[0]:
            raise ValueError("Target association changed")
        return await actual(db, command)

    monkeypatch.setattr(service, "import_local_source", changed)
    run = await create_run(session, media.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item_id = await session.scalar(select(CompanionImportItem.id))
    await session.commit()
    router = SimpleNamespace(queue_for=AsyncMock())
    for _ in range(3):
        await process_item(maker(session), router, item_id)
    async with maker(session)() as db:
        item = await db.get(CompanionImportItem, item_id)
        assert item.state == "unresolved" and item.had_unresolved
        assert item.tracklist_status == "found"
    assert calls == ids
    router.queue_for.assert_not_called()


async def test_continuation_ack_loss_replays_exact_durable_next_step(session, monkeypatch):
    from phaze.tasks import companion_import as task

    run = await create_run(session, "test-fileserver", associated=True)
    requested = run.continuation
    await session.commit()
    monkeypatch.setattr(task, "process_run_page", AsyncMock(return_value={"complete": False}))
    calls = []

    async def enqueue(_name, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("broker inserted, ack lost")

    ctx = {"async_session": maker(session), "task_router": object(), "queue": SimpleNamespace(enqueue=enqueue)}
    with pytest.raises(RuntimeError, match="ack lost"):
        await task.import_agent_companions(ctx, run_id=str(run.id), step=str(requested))
    await task.import_agent_companions(ctx, run_id=str(run.id), step=str(requested))
    await task.import_agent_companions(ctx, run_id=str(run.id), step=str(requested))
    assert len({call["step"] for call in calls}) == 1
    assert calls[0]["step"] != str(requested)
    task.process_run_page.assert_awaited_once()


@pytest.mark.parametrize("complete_first", [False, True])
async def test_classifier_prefers_complete_same_parent_interpretation_without_invented_chronology(session, complete_first):
    from phaze.schemas.local_source_import import ImportLocalSource
    from phaze.services.companion_import_backfill import classify_source
    from phaze.services.local_source_import import import_local_source
    from phaze.tracklist_providers.domain import LoadBudget

    media, companion, raw = await inventory(session)
    await features(session, companion)
    budgets = (LoadBudget(), LoadBudget(max_tracks=1)) if complete_first else (LoadBudget(max_tracks=1), LoadBudget())
    observations = []
    for budget in budgets:
        result = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw, budget=budget))
        observations.append(await session.get(ProviderSourceObservation, result.tracklist_observation_id))
    assert {observation.status for observation in observations} == {"incomplete", "found"}
    assert {observation.parent_id for observation in observations} == {raw}
    assert len({observation.parser_version for observation in observations}) == 1
    assert len({observation.retrieved_at for observation in observations}) == 1
    assert {len(observation.payload["tracks"]) for observation in observations} == {1, 2}
    assert (await classify_source(session, companion))[0] == "current"
    assert await session.scalar(select(func.count()).select_from(ProviderRecordingSelection)) == 0
    await store_capture_report(session, companion.agent_id, report(companion))
    companion.sha256_hash = "f" * 64
    feat = await session.get(CompanionContentFeatures, companion.id)
    feat.fingerprint = companion.sha256_hash
    await session.flush()
    assert await classify_source(session, companion) == ("stale", "inventory_changed")


async def test_worker_quarantine_refusal_never_opens(monkeypatch):
    from phaze.schemas.agent_companion_capture import CaptureBudget
    from phaze.services import companion_capture_worker as worker

    opened = []
    monkeypatch.setattr(worker, "_open_regular", lambda path: opened.append(path))
    target = CaptureTarget(file_id=uuid.uuid4(), path="/synthetic/.phaze-quarantine/notes.txt", expected_sha256="a" * 64, expected_size=0)
    assert worker.read_capture(target, CaptureBudget(), ["/synthetic"]).code == "quarantined"
    assert opened == []
