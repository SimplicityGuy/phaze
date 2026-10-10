"""Supported operator/controller recovery across real database and broker boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.models.agent import Agent
from phaze.models.companion_import import CompanionImportItem, CompanionImportRun
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch
from phaze.services.companion_import_backfill import classify_source, create_run, enumerate_page, process_item, run_status
from phaze.tasks._shared.queue_factory import build_pipeline_queue
from tests.db_guard import integration_dsns
from tests.integration.test_companion_import_backfill import features, maker, report
from tests.integration.test_local_source_import import inventory


async def test_operator_dry_run_all_pages_and_real_broker_ack_loss_resume(async_engine, monkeypatch, capsys, tmp_path):
    from phaze.cli import companion_import as cli

    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    owners = [f"operator-{uuid.uuid4().hex[:8]}" for _ in range(2)]
    files = [
        FileRecord(
            id=uuid.uuid4(),
            agent_id=owners[index % 2],
            original_path=str(tmp_path / f"notes-{index}.txt"),
            current_path=str(tmp_path / f"notes-{index}.txt"),
            original_filename=f"notes-{index}.txt",
            file_type="txt",
            sha256_hash="a" * 64,
            file_size=0,
        )
        for index in range(5)
    ]
    queue_name = f"operator-import-{uuid.uuid4().hex[:8]}"
    broker, _ = integration_dsns()
    monkeypatch.setattr(cli, "async_session", factory)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(queue_url=broker, redis_url=os.environ["PHAZE_REDIS_URL"]))
    lost = False

    def queue_factory(_name, *args, **kwargs):
        queue = build_pipeline_queue(queue_name, *args, **kwargs)
        enqueue = queue.enqueue

        async def insert_then_lose_ack(*args, **kwargs):
            nonlocal lost
            job = await enqueue(*args, **kwargs)
            if not lost:
                lost = True
                raise RuntimeError("broker inserted job, acknowledgement lost")
            return job

        queue.enqueue = insert_then_lose_ack
        return queue

    monkeypatch.setattr(cli, "build_pipeline_queue", queue_factory)
    options = {"agent_id": None, "run_id": None, "page_size": 1, "after": None}
    try:
        async with factory.begin() as db:
            db.add_all([Agent(id=owner, name=owner, kind="fileserver", scan_roots=[]) for owner in owners])
            await db.flush()
            db.add_all(files)
        assert await cli.run_companion_import(apply=False, **options) == 0
        counts = json.loads(capsys.readouterr().out)
        assert counts["pending"] == 5 and sum(counts.values()) == 5
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(CompanionImportRun)) == 0
            assert await db.scalar(select(func.count()).select_from(CompanionImportItem)) == 0
        # One owner's ambiguous acknowledgement does not prevent the next owner.
        assert await cli.run_companion_import(apply=True, **options) == 1
        requests = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert {row["agent_id"] for row in requests} == set(owners)
        assert {row["status"] for row in requests} == {"requested", "enqueue_unconfirmed"}
        identity = uuid.UUID(requests[0]["run_id"])
        async with factory.begin() as db:
            await enumerate_page(db, identity, page_size=1)
        assert await cli.run_companion_import(apply=False, **{**options, "run_id": identity}) == 0
        status, items = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert status["run_id"] == str(identity) and len(items) == 1
        assert await cli.run_companion_import(apply=True, **{**options, "run_id": identity}) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "resume_requested"
        async with factory() as db:
            jobs = await db.scalar(text("SELECT count(*) FROM saq_jobs WHERE queue=:queue"), {"queue": queue_name})
            assert jobs == 2, "resume must reuse the durable run continuation rather than duplicate broker work"
            assert await db.scalar(select(func.count()).select_from(CompanionImportRun)) == 2
        assert await cli.run_companion_import(apply=False, **{**options, "page_size": 0}) == 1
        assert "--page-size" in capsys.readouterr().err
        assert await cli.run_companion_import(apply=True, **{**options, "agent_id": owners[0]}) == 0
        scoped = json.loads(capsys.readouterr().out)
        assert scoped["agent_id"] == owners[0] and scoped["status"] == "requested"
    finally:
        async with factory.begin() as db:
            run_ids = select(CompanionImportRun.id).where(CompanionImportRun.agent_id.in_(owners))
            await db.execute(delete(CompanionImportItem).where(CompanionImportItem.run_id.in_(run_ids)))
            await db.execute(delete(CompanionImportRun).where(CompanionImportRun.agent_id.in_(owners)))
            await db.execute(delete(FileRecord).where(FileRecord.id.in_([row.id for row in files])))
            await db.execute(delete(Agent).where(Agent.id.in_(owners)))
            if await db.scalar(text("SELECT to_regclass('saq_jobs')")) is not None:
                await db.execute(text("DELETE FROM saq_jobs WHERE queue=:queue"), {"queue": queue_name})


async def test_automatic_association_commits_one_run_before_real_keyed_broker_enqueue(session):
    from phaze.tasks.companion_association import associate_agent_companions

    media, companion, _raw = await inventory(session)
    await features(session, companion)
    await session.commit()
    broker, _ = integration_dsns()
    queue_name = f"association-import-{uuid.uuid4().hex[:8]}"
    queue = build_pipeline_queue(queue_name, broker, cache_redis_url=os.environ["PHAZE_REDIS_URL"], ledger_sessionmaker=maker(session))
    await queue.connect()
    try:
        ctx = {"async_session": maker(session), "queue": queue}
        first = await associate_agent_companions(ctx, agent_id=media.agent_id, window=123)
        second = await associate_agent_companions(ctx, agent_id=media.agent_id, window=123)
        assert first["status"] == second["status"] == "associated"
        async with maker(session)() as db:
            runs = list(await db.scalars(select(CompanionImportRun)))
            assert len(runs) == 1 and runs[0].associated
            assert runs[0].request_key == f"association:{media.agent_id}:123"
            assert await db.scalar(text("SELECT count(*) FROM saq_jobs WHERE queue=:queue"), {"queue": queue_name}) == 1
            assert await db.scalar(select(func.count()).select_from(CompanionImportItem)) == 0
    finally:
        await queue.disconnect()
        await queue.cache_redis.aclose()
        async with maker(session).begin() as db:
            await db.execute(text("DELETE FROM saq_jobs WHERE queue=:queue"), {"queue": queue_name})


async def test_new_eligible_inventory_without_retained_capture_requests_owner_read(session):
    companion = FileRecord(
        id=uuid.uuid4(),
        agent_id="test-fileserver",
        original_path="/synthetic/new-notes.txt",
        current_path="/synthetic/new-notes.txt",
        original_filename="new-notes.txt",
        file_type="txt",
        sha256_hash="a" * 64,
        file_size=10,
    )
    session.add(companion)
    await session.flush()
    await features(session, companion)
    assert await classify_source(session, companion) == ("pending", "needs_capture")
    run = await create_run(session, companion.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item_id = await session.scalar(select(CompanionImportItem.id))
    await session.commit()
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    await process_item(maker(session), SimpleNamespace(queue_for=lambda *_: queue), item_id)
    queue.enqueue.assert_awaited_once()
    async with maker(session)() as db:
        item = await db.get(CompanionImportItem, item_id)
        assert item.state == "awaiting_capture" and item.code == "capture_queued"


async def test_controller_defers_scan_then_derives_links_and_imports_retained_text(session):
    from phaze.tasks.companion_import import import_agent_companions

    media, companion, _raw = await inventory(session)
    await features(session, companion)
    scan = ScanBatch(id=uuid.uuid4(), agent_id=media.agent_id, scan_path="/synthetic", status="running")
    session.add(scan)
    run = await create_run(session, media.agent_id)
    first_step = run.continuation
    await session.commit()
    queue = SimpleNamespace(enqueue=AsyncMock())
    agent_queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    ctx = {"async_session": maker(session), "queue": queue, "task_router": SimpleNamespace(queue_for=lambda *_: agent_queue)}
    deferred = await import_agent_companions(ctx, run_id=str(run.id), step=str(first_step))
    assert deferred["status"] == "deferred"
    async with maker(session).begin() as db:
        (await db.get(ScanBatch, scan.id)).status = "completed"
    for _ in range(4):
        async with maker(session)() as db:
            step = (await db.get(CompanionImportRun, run.id)).continuation
        status = await import_agent_companions(ctx, run_id=str(run.id), step=str(step))
        if status["complete"]:
            break
    assert status["complete"] and sum(status["counts"].values()) == 1
    async with maker(session)() as db:
        assert (await db.get(CompanionImportRun, run.id)).associated
        assert (await db.scalar(select(CompanionImportItem))).state in {"current", "unresolved"}
    agent_queue.enqueue.assert_not_awaited()
    with pytest.raises(ValueError, match="Unknown"):
        await import_agent_companions(ctx, run_id=str(uuid.uuid4()), step=str(uuid.uuid4()))


@pytest.mark.parametrize("failure", ["deleted", "stale", "callback", "timeout"])
async def test_recovery_outcomes_remain_visible_without_owner_reads(session, failure):
    from phaze.services.companion_capture import store_capture_report

    _media, companion, _raw = await inventory(session)
    await features(session, companion)
    run = await create_run(session, companion.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item = await session.scalar(select(CompanionImportItem))
    if failure == "deleted":
        await session.delete(companion)
    elif failure == "stale":
        companion.sha256_hash = "b" * 64
    else:
        item.state = "awaiting_capture"
        item.dispatched_at = datetime.now(UTC) - timedelta(minutes=16 if failure == "timeout" else 1)
        if failure == "callback":
            await store_capture_report(session, companion.agent_id, report(companion, status="unavailable"))
    item_id = item.id
    await session.commit()
    queue = SimpleNamespace(connect=AsyncMock(), enqueue=AsyncMock())
    await process_item(maker(session), SimpleNamespace(queue_for=lambda *_: queue), item_id)
    async with maker(session)() as db:
        status = await run_status(db, run.id)
        final = await db.get(CompanionImportItem, item_id)
        assert (
            final.code == {"deleted": "deleted", "stale": "inventory_changed", "callback": "capture_outcome", "timeout": "capture_timeout"}[failure]
        )
        assert status["complete"]
    queue.enqueue.assert_not_awaited()


async def test_active_lease_and_expired_reclaim_fence_late_enqueue_acknowledgement(session):
    from phaze.services.companion_capture import store_capture_report

    _media, companion, _raw = await inventory(session)
    await features(session, companion)
    await store_capture_report(session, companion.agent_id, report(companion, status="unavailable"))
    run = await create_run(session, companion.agent_id, associated=True)
    await enumerate_page(session, run.id)
    item_id = await session.scalar(select(CompanionImportItem.id))
    await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def enqueue(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("late acknowledgement from expired lease")

    queue = SimpleNamespace(connect=AsyncMock(), enqueue=enqueue)
    router = SimpleNamespace(queue_for=lambda *_: queue)
    now = datetime.now(UTC)
    first = asyncio.create_task(process_item(maker(session), router, item_id, now=now))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await process_item(maker(session), router, item_id, now=now + timedelta(seconds=1))
        assert calls == 1, "active lease must not issue duplicate physical work"
        await process_item(maker(session), router, item_id, now=now + timedelta(seconds=31))
        assert calls == 2
        release.set()
        await asyncio.wait_for(first, timeout=5)
        async with maker(session)() as db:
            item = await db.get(CompanionImportItem, item_id)
            assert item.code == "capture_queued" and item.attempts == 2 and item.lease_until is None
    finally:
        release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)


async def test_controller_held_association_lock_defers_without_import(session, async_engine):
    from phaze.services.companion_autolink import agent_association_lock
    from phaze.tasks.companion_import import import_agent_companions

    run = await create_run(session, "test-fileserver")
    requested = run.continuation
    await session.commit()
    queue = SimpleNamespace(enqueue=AsyncMock())
    ctx = {"async_session": maker(session), "queue": queue, "task_router": object()}
    other = async_sessionmaker(async_engine, expire_on_commit=False)
    async with other() as db, agent_association_lock(db, "test-fileserver") as acquired:
        assert acquired
        result = await import_agent_companions(ctx, run_id=str(run.id), step=str(requested))
    assert result["status"] == "deferred"
    async with maker(session)() as db:
        current = await db.get(CompanionImportRun, run.id)
        assert not current.associated and current.continuation != requested
        assert await db.scalar(select(func.count()).select_from(CompanionImportItem)) == 0
    queue.enqueue.assert_awaited_once()


@pytest.mark.parametrize(
    "condition,expected",
    [("quarantine", "quarantined"), ("ambiguous", "ambiguous"), ("junk", "junk"), ("features", "pending"), ("unsupported", "unsupported")],
)
async def test_dry_run_eligibility_distinguishes_excluded_inventory_and_capture_status(session, condition, expected):
    from phaze.models.companion_content import CompanionContentFeatures
    from phaze.services.companion_capture import store_capture_report

    _media, companion, _raw = await inventory(session)
    await features(session, companion)
    if condition == "quarantine":
        companion.original_path = "/synthetic/.phaze-quarantine/notes.txt"
    elif condition == "ambiguous":
        companion.companion_ambiguous_at = datetime.now(UTC)
    elif condition == "junk":
        (await session.get(CompanionContentFeatures, companion.id)).junk_class = "site_ad"
    elif condition == "features":
        (await session.get(CompanionContentFeatures, companion.id)).fingerprint = "b" * 64
    else:
        await store_capture_report(session, companion.agent_id, report(companion, status="unsupported"))
    await session.flush()
    assert (await classify_source(session, companion))[0] == expected
