"""Integration coverage for Metadata workspace measurement semantics."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from bs4 import BeautifulSoup
from httpx import AsyncClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.enums.stage import Stage
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.pipeline import _build_dag_context
from phaze.services import pipeline as pipeline_service
from phaze.services.pipeline import (
    MetadataActivitySummary,
    MetadataSelectionSummary,
    MetadataStatusSnapshot,
    PendingFilesPage,
    StageActivitySnapshot,
    get_metadata_activity_summary,
    get_metadata_pending_files,
    get_metadata_selection_summary,
    get_metadata_status_snapshot,
    get_pending_files_page,
    get_stage_activity_snapshot,
    get_stage_progress,
)
import phaze.services.pipeline.buckets as pipeline_buckets
import phaze.services.pipeline.pending as pipeline_pending
import phaze.services.pipeline.stages as pipeline_stages


pytestmark = pytest.mark.integration


def _file(name: str) -> FileRecord:
    return FileRecord(
        agent_id="test-fileserver",
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/test/music/{name}",
        original_filename=name,
        current_path=f"/test/music/{name}",
        file_type="mp3",
        file_size=1,
    )


@pytest.mark.asyncio
async def test_eligible_measurement_matches_extract_all_selection(session: AsyncSession) -> None:
    ready = _file("ready.mp3")
    failed = _file("failed.mp3")
    done = _file("done.mp3")
    inflight = _file("inflight.mp3")
    resolved = _file("resolved.mp3")
    canonical = _file("canonical.mp3")
    session.add_all([ready, failed, done, inflight, resolved, canonical])
    await session.flush()
    now = datetime.now(UTC)
    session.add_all(
        [
            FileMetadata(file_id=failed.id, failed_at=now),
            FileMetadata(file_id=done.id, failed_at=None),
            SchedulingLedger(
                key=f"extract_file_metadata:{inflight.id}",
                function="extract_file_metadata",
                routing="local",
                payload={},
            ),
            DedupResolution(file_id=resolved.id, canonical_file_id=canonical.id),
        ]
    )
    await session.flush()

    pending_ids = {row.id for row in await get_metadata_pending_files(session)}
    summary = await get_metadata_selection_summary(session)

    assert pending_ids == {ready.id, failed.id, canonical.id}
    assert summary.available is True
    assert summary.eligible_count == len(pending_ids)


@pytest.mark.asyncio
async def test_recent_activity_counts_successful_writes_only(session: AsyncSession) -> None:
    recent = _file("recent-success.mp3")
    old = _file("old-success.mp3")
    failed = _file("recent-failure.mp3")
    session.add_all([recent, old, failed])
    await session.flush()
    now = datetime.now(UTC)
    recent_metadata = FileMetadata(file_id=recent.id, failed_at=None, updated_at=now)
    session.add_all(
        [
            recent_metadata,
            FileMetadata(file_id=old.id, failed_at=None, updated_at=now - timedelta(days=2)),
            FileMetadata(file_id=failed.id, failed_at=now, updated_at=now),
        ]
    )
    await session.flush()
    retry_at = now + timedelta(seconds=1)
    recent_metadata.title = "updated on retry"
    recent_metadata.updated_at = retry_at
    await session.flush()

    summary = await get_metadata_activity_summary(session)

    assert summary.available is True
    assert summary.unique_files_24h == 1
    assert summary.latest_successful_at == retry_at


@pytest.mark.asyncio
async def test_empty_recent_activity_is_known_zero_not_unavailable(session: AsyncSession) -> None:
    summary = await get_metadata_activity_summary(session)

    assert summary == MetadataActivitySummary(unique_files_24h=0, latest_successful_at=None, available=True)


@pytest.mark.asyncio
async def test_status_read_failure_recovers_on_isolated_retry(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    done = _file("status-retry.mp3")
    session.add(done)
    await session.flush()
    session.add(FileMetadata(file_id=done.id, failed_at=None))
    await session.flush()

    with monkeypatch.context() as failure:
        failure.setattr(
            pipeline_buckets,
            "_metadata_status_stmt",
            lambda: select(text("1"), text("1")).select_from(text("metadata_status_retry_missing")),
        )
        degraded = await get_metadata_status_snapshot(session)
    retried = await get_metadata_status_snapshot(session)

    assert degraded == MetadataStatusSnapshot()
    assert retried.available is True
    assert retried.done == 1
    assert retried.total == 1


@pytest.mark.asyncio
async def test_selection_read_failure_recovers_on_isolated_retry(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    session.add(_file("selection-retry.mp3"))
    await session.flush()

    with monkeypatch.context() as failure:
        failure.setattr(
            pipeline_pending,
            "_metadata_pending_stmt",
            lambda: select(FileRecord).where(text("EXISTS (SELECT 1 FROM metadata_selection_retry_missing)")),
        )
        degraded = await get_metadata_selection_summary(session)
    retried = await get_metadata_selection_summary(session)

    assert degraded == MetadataSelectionSummary()
    assert retried == MetadataSelectionSummary(eligible_count=1, available=True)


@pytest.mark.asyncio
async def test_pending_page_failure_recovers_on_isolated_retry(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = _file("pending-page-retry.mp3")
    session.add(pending)
    await session.flush()

    with monkeypatch.context() as failure:
        failure.setattr(
            pipeline_pending,
            "_metadata_pending_stmt",
            lambda: select(FileRecord).where(text("EXISTS (SELECT 1 FROM metadata_pending_page_retry_missing)")),
        )
        degraded = await get_pending_files_page(session, Stage.METADATA)
    retried = await get_pending_files_page(session, Stage.METADATA)

    assert degraded.available is False
    assert degraded.rows == []
    assert retried.available is True
    assert [row.id for row in retried.rows] == [pending.id]


@pytest.mark.asyncio
async def test_recent_write_failure_recovers_on_isolated_retry(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _file("activity-retry.mp3")
    session.add(recent)
    await session.flush()
    session.add(FileMetadata(file_id=recent.id, failed_at=None, updated_at=datetime.now(UTC)))
    await session.flush()

    with monkeypatch.context() as failure:
        failure.setattr(
            pipeline_pending,
            "_metadata_activity_stmt",
            lambda _cutoff: select(text("1"), text("NULL")).select_from(text("metadata_activity_retry_missing")),
        )
        degraded = await get_metadata_activity_summary(session)
    retried = await get_metadata_activity_summary(session)

    assert degraded == MetadataActivitySummary()
    assert retried.available is True
    assert retried.unique_files_24h == 1


@pytest.mark.asyncio
async def test_stage_activity_preserves_saq_queued_and_active_states(session: AsyncSession) -> None:
    await session.execute(text("CREATE TEMP TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL) ON COMMIT DROP"))
    await session.execute(
        text(
            "INSERT INTO saq_jobs (key, status) VALUES "
            "('extract_file_metadata:queued', 'queued'), "
            "('extract_file_metadata:active', 'active'), "
            "('extract_file_metadata:complete', 'complete')"
        )
    )

    snapshot = await get_stage_activity_snapshot(session)

    assert snapshot.available is True
    assert snapshot.counts["metadata"] == {"queued": 1, "active": 1}


@pytest.mark.asyncio
async def test_queue_read_failure_recovers_on_isolated_retry(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await session.execute(text("CREATE TEMP TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL) ON COMMIT DROP"))
    await session.execute(text("INSERT INTO saq_jobs (key, status) VALUES ('extract_file_metadata:retry', 'queued')"))

    with monkeypatch.context() as failure:
        failure.setattr(pipeline_stages, "_STAGE_ACTIVITY_SQL", text("SELECT * FROM metadata_queue_retry_missing"))
        degraded = await get_stage_activity_snapshot(session)
    retried = await get_stage_activity_snapshot(session)

    assert degraded.available is False
    assert retried.available is True
    assert retried.counts["metadata"] == {"queued": 1, "active": 0}


@pytest.mark.asyncio
async def test_measurement_read_failures_are_explicitly_unavailable(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_stages, "_STAGE_ACTIVITY_SQL", text("SELECT * FROM metadata_queue_measurement_missing"))
    monkeypatch.setattr(
        pipeline_pending,
        "_metadata_pending_stmt",
        lambda: select(FileRecord).where(text("EXISTS (SELECT 1 FROM metadata_selection_measurement_missing)")),
    )
    monkeypatch.setattr(
        pipeline_buckets,
        "_metadata_status_stmt",
        lambda: select(text("1"), text("1")).select_from(text("metadata_status_measurement_missing")),
    )
    monkeypatch.setattr(
        pipeline_pending,
        "_metadata_activity_stmt",
        lambda _cutoff: select(text("1"), text("NULL")).select_from(text("metadata_activity_measurement_missing")),
    )

    queue = await get_stage_activity_snapshot(session)
    selection = await get_metadata_selection_summary(session)
    status = await get_metadata_status_snapshot(session)
    recent = await get_metadata_activity_summary(session)

    assert queue.available is False
    assert selection == MetadataSelectionSummary(eligible_count=None, available=False)
    assert status == MetadataStatusSnapshot()
    assert recent == MetadataActivitySummary(unique_files_24h=None, latest_successful_at=None, available=False)
    assert (await session.execute(select(1))).scalar_one() == 1


@pytest.mark.asyncio
async def test_stats_poll_reuses_one_availability_bearing_metadata_aggregation(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = pipeline_service._metadata_status_stmt

    def tracked_status_stmt():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(pipeline_buckets, "_metadata_status_stmt", tracked_status_stmt)

    progress = await get_stage_progress(session)
    context = await _build_dag_context(SimpleNamespace(), session, {}, progress)

    assert calls == 1
    assert context["dag"]["metadataStatusKnown"] == int(progress["metadata"]["available"] or 0)
    assert context["dag"]["metadataStatusDone"] == int(progress["metadata"]["done"] or 0)


@pytest.mark.asyncio
async def test_workspace_renders_selection_and_saq_states_without_execution_claim(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = StageActivitySnapshot(
        counts={"metadata": {"queued": 4, "active": 2}, "analyze": {"queued": 0, "active": 0}},
        available=True,
    )
    recent = MetadataActivitySummary(unique_files_24h=7, latest_successful_at=datetime.now(UTC), available=True)
    monkeypatch.setattr(
        "phaze.routers.shell.stage_context.get_metadata_selection_summary",
        AsyncMock(return_value=MetadataSelectionSummary(eligible_count=3, available=True)),
    )
    monkeypatch.setattr(
        "phaze.routers.shell.stage_context.get_metadata_status_snapshot",
        AsyncMock(return_value=MetadataStatusSnapshot(done=5, failed=1, total=9, available=True)),
    )
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_stage_activity_snapshot", AsyncMock(return_value=queue))
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_metadata_activity_summary", AsyncMock(return_value=recent))

    response = await client.get("/s/metadata", headers={"HX-Request": "true"})

    assert response.status_code == 200
    document = BeautifulSoup(response.text, "html.parser")
    subcount = document.find(id="stage-workspace-subcount")
    assert subcount is not None
    assert "metadataEligibleKnown" in subcount.find("span")["x-text"]
    assert 'id="metadata-eligible-value"' in response.text and ">3</span>" in response.text
    assert 'id="metadata-queued-value"' in response.text and ">4</span>" in response.text
    assert 'id="metadata-active-value"' in response.text and ">2</span>" in response.text
    assert 'id="metadata-completed-value"' in response.text and ">5</span>" in response.text
    assert 'id="metadata-failed-value"' in response.text and ">1</span>" in response.text
    assert "SAQ active; may be claimed or executing" in response.text
    assert "active means claimed by queue processing and does not prove user code is currently executing" in response.text
    assert "Metadata: running" not in response.text


@pytest.mark.asyncio
async def test_workspace_renders_unknown_for_degraded_measurements(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown_queue = StageActivitySnapshot(
        counts={"metadata": {"queued": 0, "active": 0}, "analyze": {"queued": 0, "active": 0}},
        available=False,
    )
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_metadata_selection_summary", AsyncMock(return_value=MetadataSelectionSummary()))
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_metadata_status_snapshot", AsyncMock(return_value=MetadataStatusSnapshot()))
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_stage_activity_snapshot", AsyncMock(return_value=unknown_queue))
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_metadata_activity_summary", AsyncMock(return_value=MetadataActivitySummary()))

    response = await client.get("/s/metadata", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "Metadata measurements degraded" in response.text
    assert "Recent activity unavailable" in response.text
    assert "Unknown values display as an em dash; they are not zero" in response.text
    assert 'id="metadata-eligible-value"' in response.text and ">—</span>" in response.text
    assert 'id="metadata-completed-value"' in response.text and ">—</span>" in response.text
    assert 'id="metadata-failed-value"' in response.text and ">—</span>" in response.text
    assert "SAQ active; may be claimed or executing" in response.text
    assert "does not prove user code is currently executing" in response.text
    assert "Most recent successful run" not in response.text


@pytest.mark.asyncio
async def test_pending_fragment_renders_failure_then_truthful_empty_retry(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = AsyncMock(
        side_effect=[
            PendingFilesPage(rows=[], page=1, page_size=50, has_next=False, available=False),
            PendingFilesPage(rows=[], page=1, page_size=50, has_next=False, available=True),
        ]
    )
    monkeypatch.setattr("phaze.routers.pipeline.files.get_pending_files_page", pages)

    degraded = await client.get("/pipeline/pending-files?stage=metadata", headers={"HX-Request": "true"})
    retried = await client.get("/pipeline/pending-files?stage=metadata", headers={"HX-Request": "true"})

    assert degraded.status_code == 200
    assert "Eligible files unavailable" in degraded.text
    assert "current selection is unknown, not empty" in degraded.text
    assert 'hx-get="/pipeline/pending-files?stage=metadata&amp;page=1&amp;page_size=50&amp;sort=filename&amp;order=asc"' in degraded.text
    assert "Extract All currently selects no files" not in degraded.text
    assert retried.status_code == 200
    assert "Eligible files unavailable" not in retried.text
    assert "Extract All currently selects no files" in retried.text


@pytest.mark.asyncio
async def test_shell_rail_marks_metadata_status_unavailable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = StageActivitySnapshot(
        counts={"metadata": {"queued": 0, "active": 0}, "analyze": {"queued": 0, "active": 0}},
        available=True,
    )
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_metadata_selection_summary", AsyncMock(return_value=MetadataSelectionSummary(0, True)))
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_metadata_status_snapshot", AsyncMock(return_value=MetadataStatusSnapshot()))
    monkeypatch.setattr("phaze.routers.shell.stage_context.get_stage_activity_snapshot", AsyncMock(return_value=queue))
    monkeypatch.setattr(
        "phaze.routers.shell.stage_context.get_metadata_activity_summary",
        AsyncMock(return_value=MetadataActivitySummary(0, None, True)),
    )

    response = await client.get("/s/metadata")

    assert response.status_code == 200
    assert "$store.pipeline.metadataStatusKnown ?" in response.text
    assert "$store.pipeline.metadataStatusDone" in response.text
    assert "Metadata status unavailable" in response.text
    assert ">— / —</span>" in response.text
