"""Integration coverage for Metadata workspace measurement semantics."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
import uuid

from bs4 import BeautifulSoup
from httpx import AsyncClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services import pipeline as pipeline_service
from phaze.services.pipeline import (
    MetadataActivitySummary,
    MetadataSelectionSummary,
    StageActivitySnapshot,
    get_metadata_activity_summary,
    get_metadata_pending_files,
    get_metadata_selection_summary,
    get_stage_activity_snapshot,
)


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
    session.add_all(
        [
            FileMetadata(file_id=recent.id, failed_at=None, updated_at=now),
            FileMetadata(file_id=old.id, failed_at=None, updated_at=now - timedelta(days=2)),
            FileMetadata(file_id=failed.id, failed_at=now, updated_at=now),
        ]
    )
    await session.flush()

    summary = await get_metadata_activity_summary(session)

    assert summary.available is True
    assert summary.successful_writes_24h == 1
    assert summary.latest_successful_at == now


@pytest.mark.asyncio
async def test_empty_recent_activity_is_known_zero_not_unavailable(session: AsyncSession) -> None:
    summary = await get_metadata_activity_summary(session)

    assert summary == MetadataActivitySummary(successful_writes_24h=0, latest_successful_at=None, available=True)


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
async def test_measurement_read_failures_are_explicitly_unavailable(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline_service, "_STAGE_ACTIVITY_SQL", text("SELECT * FROM metadata_queue_measurement_missing"))
    monkeypatch.setattr(
        pipeline_service,
        "_metadata_pending_stmt",
        lambda: select(FileRecord).where(text("EXISTS (SELECT 1 FROM metadata_selection_measurement_missing)")),
    )
    monkeypatch.setattr(
        pipeline_service,
        "_metadata_activity_stmt",
        lambda _cutoff: select(text("1"), text("NULL")).select_from(text("metadata_activity_measurement_missing")),
    )

    queue = await get_stage_activity_snapshot(session)
    selection = await get_metadata_selection_summary(session)
    recent = await get_metadata_activity_summary(session)

    assert queue.available is False
    assert selection == MetadataSelectionSummary(eligible_count=None, available=False)
    assert recent == MetadataActivitySummary(successful_writes_24h=None, latest_successful_at=None, available=False)
    assert (await session.execute(select(1))).scalar_one() == 1


@pytest.mark.asyncio
async def test_workspace_renders_selection_and_saq_states_without_execution_claim(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = StageActivitySnapshot(
        counts={"metadata": {"queued": 4, "active": 2}, "analyze": {"queued": 0, "active": 0}},
        available=True,
    )
    recent = MetadataActivitySummary(successful_writes_24h=7, latest_successful_at=datetime.now(UTC), available=True)
    monkeypatch.setattr(
        "phaze.routers.shell.get_metadata_selection_summary",
        AsyncMock(return_value=MetadataSelectionSummary(eligible_count=3, available=True)),
    )
    monkeypatch.setattr("phaze.routers.shell.get_stage_activity_snapshot", AsyncMock(return_value=queue))
    monkeypatch.setattr("phaze.routers.shell.get_metadata_activity_summary", AsyncMock(return_value=recent))

    response = await client.get("/s/metadata", headers={"HX-Request": "true"})

    assert response.status_code == 200
    document = BeautifulSoup(response.text, "html.parser")
    subcount = document.find(id="stage-workspace-subcount")
    assert subcount is not None
    assert "metadataEligibleKnown" in subcount.find("span")["x-text"]
    assert 'id="metadata-eligible-value"' in response.text and ">3</span>" in response.text
    assert 'id="metadata-queued-value"' in response.text and ">4</span>" in response.text
    assert 'id="metadata-active-value"' in response.text and ">2</span>" in response.text
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
    monkeypatch.setattr("phaze.routers.shell.get_metadata_selection_summary", AsyncMock(return_value=MetadataSelectionSummary()))
    monkeypatch.setattr("phaze.routers.shell.get_stage_activity_snapshot", AsyncMock(return_value=unknown_queue))
    monkeypatch.setattr("phaze.routers.shell.get_metadata_activity_summary", AsyncMock(return_value=MetadataActivitySummary()))

    response = await client.get("/s/metadata", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "Metadata measurements degraded" in response.text
    assert "Recent activity unavailable" in response.text
    assert "Unknown values display as an em dash; they are not zero" in response.text
    assert 'id="metadata-eligible-value"' in response.text and ">—</span>" in response.text
    assert "SAQ active; may be claimed or executing" in response.text
    assert "does not prove user code is currently executing" in response.text
    assert "Most recent successful run" not in response.text
