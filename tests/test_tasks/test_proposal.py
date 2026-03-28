"""Tests for the proposal arq task function."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from arq import Retry
import pytest

from phaze.services.proposal import BatchProposalResponse, FileProposalResponse


def _make_ctx(job_try: int = 1) -> dict[str, Any]:
    """Create a minimal arq context dict with mocked services."""
    return {
        "job_try": job_try,
        "redis": AsyncMock(),
        "proposal_service": AsyncMock(),
    }


def _make_file_record(
    file_id: uuid.UUID | None = None,
    file_type: str = "mp3",
    state: str = "analyzed",
) -> MagicMock:
    """Create a mock FileRecord."""
    record = MagicMock()
    record.id = file_id or uuid.uuid4()
    record.file_type = file_type
    record.state = state
    record.original_filename = "track.mp3"
    record.original_path = "/music/track.mp3"
    record.current_path = "/music/track.mp3"
    return record


def _make_analysis() -> MagicMock:
    """Create a mock AnalysisResult."""
    analysis = MagicMock()
    analysis.bpm = 128.0
    analysis.musical_key = "Am"
    analysis.mood = "dark"
    analysis.style = "techno"
    analysis.features = {"energy": 0.85}
    return analysis


SAMPLE_BATCH_RESPONSE = BatchProposalResponse(
    proposals=[
        FileProposalResponse(
            file_index=0,
            proposed_filename="Artist - Live @ Event.mp3",
            confidence=0.9,
            reasoning="good metadata",
        )
    ]
)


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal._get_session", new_callable=AsyncMock)
async def test_generate_proposals_happy_path(
    mock_get_session: AsyncMock,
    mock_companions: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """generate_proposals loads files, calls LLM, stores proposals, returns ok status."""
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)
    analysis = _make_analysis()

    session = AsyncMock()
    mock_get_session.return_value = session

    # First execute: FileRecord, Second: AnalysisResult
    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_analysis = MagicMock()
    mock_result_analysis.scalar_one_or_none.return_value = analysis
    session.execute.side_effect = [mock_result_file, mock_result_analysis]

    mock_companions.return_value = []

    ctx = _make_ctx()
    ctx["proposal_service"].generate_batch.return_value = SAMPLE_BATCH_RESPONSE
    mock_store.return_value = 1

    result = await generate_proposals(ctx, [str(file_id)], batch_index=0)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["batch"] == 0
    mock_rate_limit.assert_called_once()
    ctx["proposal_service"].generate_batch.assert_called_once()
    mock_store.assert_called_once()
    session.commit.assert_called_once()


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal._get_session", new_callable=AsyncMock)
async def test_generate_proposals_file_not_found(
    mock_get_session: AsyncMock,
    mock_companions: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """generate_proposals returns empty status when no files found in DB."""
    from phaze.tasks.proposal import generate_proposals

    session = AsyncMock()
    mock_get_session.return_value = session

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    ctx = _make_ctx()
    result = await generate_proposals(ctx, [str(uuid.uuid4())], batch_index=0)

    assert result["status"] == "empty"
    assert result["count"] == 0
    mock_rate_limit.assert_not_called()


@patch("phaze.tasks.proposal._get_session", new_callable=AsyncMock)
async def test_generate_proposals_retry_on_exception(mock_get_session: AsyncMock) -> None:
    """generate_proposals raises arq Retry with defer=job_try*10 on exception."""
    from phaze.tasks.proposal import generate_proposals

    session = AsyncMock()
    mock_get_session.return_value = session
    session.execute.side_effect = RuntimeError("DB connection failed")

    ctx = _make_ctx(job_try=2)
    with pytest.raises(Retry) as exc_info:
        await generate_proposals(ctx, [str(uuid.uuid4())], batch_index=0)

    # arq stores defer as defer_score in milliseconds
    assert exc_info.value.defer_score == 2 * 10 * 1000
    session.rollback.assert_called_once()
    session.close.assert_called_once()


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal._get_session", new_callable=AsyncMock)
async def test_generate_proposals_calls_rate_limit(
    mock_get_session: AsyncMock,
    mock_companions: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """generate_proposals calls check_rate_limit with ctx redis and settings max_rpm."""
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)
    analysis = _make_analysis()

    session = AsyncMock()
    mock_get_session.return_value = session

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_analysis = MagicMock()
    mock_result_analysis.scalar_one_or_none.return_value = analysis
    session.execute.side_effect = [mock_result_file, mock_result_analysis]

    mock_companions.return_value = []

    ctx = _make_ctx()
    ctx["proposal_service"].generate_batch.return_value = SAMPLE_BATCH_RESPONSE
    mock_store.return_value = 1

    await generate_proposals(ctx, [str(file_id)], batch_index=0)

    mock_rate_limit.assert_called_once_with(ctx["redis"], 30)  # settings.llm_max_rpm default


def test_worker_settings_contains_generate_proposals() -> None:
    """WorkerSettings.functions includes generate_proposals."""
    from phaze.tasks.worker import WorkerSettings

    func_names = [f.__name__ if callable(f) else str(f) for f in WorkerSettings.functions]
    assert "generate_proposals" in func_names


def test_worker_startup_creates_proposal_service() -> None:
    """startup function initializes proposal_service in context."""
    # We verify by checking the startup function source references ProposalService
    import inspect

    from phaze.tasks.worker import startup

    source = inspect.getsource(startup)
    assert "proposal_service" in source
    assert "ProposalService" in source
