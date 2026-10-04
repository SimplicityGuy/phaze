"""A lost backfill crosses the real producer, enqueue hooks, broker, recovery and reaper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from saq import Status as JobStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.config import get_settings
from phaze.config_backends import LocalBackend
from phaze.enums.stage import Stage
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.routers.agent_analysis import report_analysis_failed
from phaze.schemas.agent_analysis import AnalysisFailurePayload
from phaze.services.reanalysis_backfill import enqueue_incomplete_reanalysis
from phaze.services.stage_status import domain_completed_clause, orphaned_clause, resolved_ledger_clause
from phaze.tasks.ledger_reaper import reap_resolved_ledger_rows
from phaze.tasks.reenqueue import _build_done_sets, is_domain_completed, recover_orphaned_work
from tests._queue_fakes import make_agent_live


if TYPE_CHECKING:
    from saq.queue.postgres import PostgresQueue
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession


@pytest.mark.parametrize("consumer", ["recovery", "reaper", "terminal_failure"])
async def test_lost_backfill_is_still_owed(
    session: AsyncSession,
    _db_connection: AsyncConnection,
    make_file: Any,
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
) -> None:
    """An old successful analysis must not satisfy a newly enqueued, then lost, backfill."""
    queue, _ = stage_env
    maker = async_sessionmaker(_db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    queue.ledger_sessionmaker = maker
    monkeypatch.setattr(get_settings(), "backends", [LocalBackend(kind="local", id="local", rank=99, cap=1)])
    await make_agent_live(session)
    file = await make_file()
    session.add(
        AnalysisResult(
            file_id=file.id,
            analysis_completed_at=datetime.now(UTC) - timedelta(days=1),
            fine_windows_analyzed=1,
            fine_windows_total=2,
            coarse_windows_analyzed=1,
            coarse_windows_total=1,
        )
    )
    await session.commit()
    router = SimpleNamespace(queue_for=lambda *_args: queue)
    outcomes = await enqueue_incomplete_reanalysis(session, SimpleNamespace(task_router=router))
    assert [outcome.outcome for outcome in outcomes] == ["queued"]
    key = f"process_file:{file.id}"
    ledger = await session.get(SchedulingLedger, key)
    assert ledger is not None, "the production enqueue hook must persist the obligation"
    assert await queue.job(key) is not None, "the real broker must hold the enqueued job"

    if consumer == "terminal_failure":
        agent = await session.get(Agent, "test-fileserver")
        assert agent is not None
        await report_analysis_failed(
            file.id,
            AnalysisFailurePayload(reason="error", error="Synthetic stale failure", attempt_enqueued_at=ledger.enqueued_at - timedelta(days=1)),
            agent,
            session,
        )
        await session.refresh(ledger)
        ledger = await session.get(SchedulingLedger, key)
        assert ledger is not None and ledger.terminal_at is None
        await report_analysis_failed(
            file.id,
            AnalysisFailurePayload(reason="error", error="Synthetic terminal failure", attempt_enqueued_at=ledger.enqueued_at),
            agent,
            session,
        )
        analysis = await session.scalar(select(AnalysisResult).where(AnalysisResult.file_id == file.id))
        assert analysis is not None and analysis.failed_at is None and analysis.analysis_completed_at is not None
        # The guarded clear cannot delete an obligation while this job is still live.
        assert await session.get(SchedulingLedger, key) is not None
        job = await queue.job(key)
        assert job is not None
        await queue.finish(job, JobStatus.FAILED, error="Synthetic terminal failure")

    # Simulate broker loss without invoking a terminal callback or clearing the ledger.
    async with queue.pool.connection() as connection:
        await connection.execute("DELETE FROM saq_jobs WHERE key = %s", (key,))
    assert await queue.job(key) is None
    ctx = {"async_session": maker, "queue": queue, "task_router": router}
    if consumer in {"recovery", "terminal_failure"}:
        result = await recover_orphaned_work(ctx, force=True)
        expected = 0 if consumer == "terminal_failure" else 1
        assert result["stages"]["process_file"]["reenqueued"] == expected
        assert (await queue.job(key) is not None) is (expected == 1)
    else:
        result = await reap_resolved_ledger_rows(ctx)
        assert result["reaped"] == 0
        assert key in set((await session.scalars(select(SchedulingLedger.key))).all())


@pytest.mark.parametrize(
    ("disposition", "complete"),
    [("before", False), ("equal", True), ("after", True), ("skipped", True), ("failed", True), ("partial", False), ("missing", False)],
)
async def test_attempt_completion_agrees_for_recovery_and_reaper(
    session: AsyncSession,
    make_file: Any,
    stage_env: tuple[PostgresQueue, async_sessionmaker[AsyncSession]],
    disposition: str,
    complete: bool,
) -> None:
    """Timestamp boundary, force-skip precedence and failure semantics agree on both sides."""
    _ = stage_env  # Real broker schema: liveness reads cannot degrade or pass vacuously.
    file = await make_file()
    enqueue_time = datetime.now(UTC)
    row = SchedulingLedger(
        key=f"process_file:{file.id}",
        function="process_file",
        routing="agent",
        payload={"file_id": str(file.id)},
        enqueued_at=enqueue_time,
    )
    session.add(row)
    if disposition != "missing":
        completion_time = None
        if disposition in {"before", "skipped"}:
            completion_time = enqueue_time - timedelta(seconds=1)
        elif disposition == "equal":
            completion_time = enqueue_time
        elif disposition == "after":
            completion_time = enqueue_time + timedelta(seconds=1)
        session.add(
            AnalysisResult(
                file_id=file.id,
                analysis_completed_at=completion_time,
                failed_at=enqueue_time - timedelta(seconds=1) if disposition == "failed" else None,
            )
        )
    if disposition == "skipped":
        session.add(StageSkip(file_id=file.id, stage="analyze", reason="Proceed without analysis"))
    await session.commit()

    facts = await _build_done_sets(session, [file.id])
    assert is_domain_completed(row, facts) is complete
    # The two SQL consumers remain complements for every scheduled, non-live file.
    from phaze.models.file import FileRecord

    assert bool(await session.scalar(select(FileRecord.id).where(FileRecord.id == file.id, resolved_ledger_clause(Stage.ANALYZE)))) is complete
    assert bool(await session.scalar(select(FileRecord.id).where(FileRecord.id == file.id, orphaned_clause(Stage.ANALYZE)))) is not complete
    if disposition == "before":
        assert await session.scalar(select(FileRecord.id).where(FileRecord.id == file.id, domain_completed_clause(Stage.ANALYZE))) == file.id


@pytest.mark.parametrize("old_success", [True, False])
async def test_stale_failure_ack_preserves_newer_lost_attempt(
    session: AsyncSession, _db_connection: AsyncConnection, make_file: Any, stage_env: Any, old_success: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ACK must neither clear a newer lost obligation nor stamp its domain as failed."""
    from phaze.services.analysis_enqueue import enqueue_process_file

    queue, _ = stage_env
    maker = async_sessionmaker(_db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    queue.ledger_sessionmaker = maker
    monkeypatch.setattr(get_settings(), "backends", [LocalBackend(kind="local", id="local", rank=99, cap=1)])
    await make_agent_live(session)
    file = await make_file()
    if old_success:
        session.add(AnalysisResult(file_id=file.id, analysis_completed_at=datetime.now(UTC) - timedelta(days=1)))
        await session.commit()
    assert await enqueue_process_file(queue, file, "test-fileserver", "/models") is not None
    ledger = await session.get(SchedulingLedger, f"process_file:{file.id}")
    assert ledger is not None
    stale_epoch = ledger.enqueued_at - timedelta(days=1)
    async with queue.pool.connection() as connection:
        await connection.execute("DELETE FROM saq_jobs WHERE key = %s", (ledger.key,))
    agent = await session.get(Agent, "test-fileserver")
    assert agent is not None
    await report_analysis_failed(
        file.id, AnalysisFailurePayload(reason="error", error="Synthetic stale ACK", attempt_enqueued_at=stale_epoch), agent, session
    )
    session.expire(ledger)
    ledger = await session.get(SchedulingLedger, f"process_file:{file.id}")
    assert ledger is not None and ledger.terminal_at is None
    analysis = await session.scalar(select(AnalysisResult).where(AnalysisResult.file_id == file.id))
    assert analysis is None if not old_success else analysis.failed_at is None and analysis.analysis_completed_at is not None
    router = SimpleNamespace(queue_for=lambda *_args: queue)
    result = await recover_orphaned_work({"async_session": maker, "queue": queue, "task_router": router}, force=True)
    assert result["stages"]["process_file"]["reenqueued"] == 1


async def test_missing_ledger_retains_token_bearing_domain_failure_report(session: AsyncSession, make_file: Any, stage_env: Any) -> None:
    """A lost ledger does not suppress best-effort reporting of a fresh terminal failure."""
    _ = stage_env
    file = await make_file()
    agent = await session.get(Agent, "test-fileserver")
    assert agent is not None
    await report_analysis_failed(
        file.id, AnalysisFailurePayload(reason="error", error="Synthetic failure", attempt_enqueued_at=datetime.now(UTC)), agent, session
    )
    analysis = await session.scalar(select(AnalysisResult).where(AnalysisResult.file_id == file.id))
    assert analysis is not None and analysis.failed_at is not None and analysis.analysis_completed_at is None
