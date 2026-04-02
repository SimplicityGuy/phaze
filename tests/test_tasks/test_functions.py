"""Tests for task functions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.tasks.functions import process_file


MOCK_ANALYSIS: dict[str, Any] = {
    "bpm": 128.0,
    "musical_key": "C minor",
    "mood": "happy",
    "style": "Electronic/House",
    "features": {"mood_acoustic": {}, "genre": {"predictions": []}},
}


def _make_session_factory(mock_session: AsyncMock) -> MagicMock:
    """Create a mock async_sessionmaker that returns a context manager yielding mock_session."""
    factory = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = cm
    return factory


def _make_ctx(mock_session: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict."""
    if mock_session is None:
        mock_session = AsyncMock()
    return {"process_pool": MagicMock(), "async_session": _make_session_factory(mock_session), "_mock_session": mock_session}


def _make_file_record(
    file_id: uuid.UUID | None = None,
    file_type: str = "mp3",
    state: str = "discovered",
    current_path: str = "/music/track.mp3",
) -> MagicMock:
    """Create a mock FileRecord."""
    record = MagicMock()
    record.id = file_id or uuid.uuid4()
    record.file_type = file_type
    record.state = state
    record.current_path = current_path
    return record


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_calls_analyze(mock_pool: AsyncMock) -> None:
    """process_file calls run_in_process_pool with analyze_file, file's current_path, and models_path."""
    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    mock_session = AsyncMock()

    # First execute returns FileRecord, second returns None (no existing AnalysisResult)
    mock_result_1 = MagicMock()
    mock_result_1.scalar_one_or_none.return_value = file_record
    mock_result_2 = MagicMock()
    mock_result_2.scalar_one_or_none.return_value = None
    mock_session.execute.side_effect = [mock_result_1, mock_result_2]

    mock_pool.return_value = MOCK_ANALYSIS

    ctx = _make_ctx(mock_session=mock_session)
    await process_file(ctx, file_id=str(file_id))

    mock_pool.assert_called_once()
    call_args = mock_pool.call_args
    # First positional arg is ctx, second is analyze_file function, third is path, fourth is models_path
    assert call_args[0][2] == "/music/track.mp3"


async def test_process_file_not_found() -> None:
    """process_file for a non-existent file_id returns status 'not_found'."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    ctx = _make_ctx(mock_session=mock_session)
    result = await process_file(ctx, file_id=str(uuid.uuid4()))

    assert result["status"] == "not_found"


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_skips_non_music(mock_pool: AsyncMock) -> None:
    """process_file for a file with file_type not music returns status 'skipped' without calling analyze."""
    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id, file_type="jpg")

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = file_record
    mock_session.execute.return_value = mock_result

    ctx = _make_ctx(mock_session=mock_session)
    result = await process_file(ctx, file_id=str(file_id))

    assert result["status"] == "skipped"
    mock_pool.assert_not_called()


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_stores_analysis_result(mock_pool: AsyncMock) -> None:
    """process_file upserts AnalysisResult with bpm, mood, style, musical_key, features from analyze_file return value."""
    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    mock_session = AsyncMock()

    mock_result_1 = MagicMock()
    mock_result_1.scalar_one_or_none.return_value = file_record
    mock_result_2 = MagicMock()
    mock_result_2.scalar_one_or_none.return_value = None  # No existing analysis
    mock_session.execute.side_effect = [mock_result_1, mock_result_2]

    mock_pool.return_value = MOCK_ANALYSIS

    ctx = _make_ctx(mock_session=mock_session)
    result = await process_file(ctx, file_id=str(file_id))

    assert result["status"] == "analyzed"
    # Verify session.add was called (new AnalysisResult created)
    mock_session.add.assert_called_once()
    added_obj = mock_session.add.call_args[0][0]
    assert added_obj.bpm == 128.0
    assert added_obj.musical_key == "C minor"
    assert added_obj.mood == "happy"
    assert added_obj.style == "Electronic/House"
    assert added_obj.features == MOCK_ANALYSIS["features"]


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_updates_file_state(mock_pool: AsyncMock) -> None:
    """process_file updates FileRecord.state to FileState.ANALYZED after successful analysis."""
    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    mock_session = AsyncMock()

    mock_result_1 = MagicMock()
    mock_result_1.scalar_one_or_none.return_value = file_record
    mock_result_2 = MagicMock()
    mock_result_2.scalar_one_or_none.return_value = None
    mock_session.execute.side_effect = [mock_result_1, mock_result_2]

    mock_pool.return_value = MOCK_ANALYSIS

    ctx = _make_ctx(mock_session=mock_session)
    await process_file(ctx, file_id=str(file_id))

    assert file_record.state == "analyzed"


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_raises_on_failure(mock_pool: AsyncMock) -> None:
    """process_file raises exception when analyze_file fails (SAQ handles retry)."""
    mock_session = AsyncMock()

    file_record = _make_file_record()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = file_record
    mock_session.execute.return_value = mock_result

    mock_pool.side_effect = RuntimeError("analysis failed")

    ctx = _make_ctx(mock_session=mock_session)
    with pytest.raises(RuntimeError, match="analysis failed"):
        await process_file(ctx, file_id=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# VALIDATION.md named tests -- ANL-01+02 pipeline integration coverage
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_analysis(mock_pool: AsyncMock) -> None:
    """ANL-01+02: process_file calls analysis via run_in_process_pool and returns 'analyzed' status."""
    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    mock_session = AsyncMock()

    mock_result_1 = MagicMock()
    mock_result_1.scalar_one_or_none.return_value = file_record
    mock_result_2 = MagicMock()
    mock_result_2.scalar_one_or_none.return_value = None
    mock_session.execute.side_effect = [mock_result_1, mock_result_2]

    mock_pool.return_value = MOCK_ANALYSIS

    ctx = _make_ctx(mock_session=mock_session)
    result = await process_file(ctx, file_id=str(file_id))

    # Pool was called with analyze_file and correct path
    mock_pool.assert_called_once()
    call_args = mock_pool.call_args[0]
    assert call_args[2] == "/music/track.mp3"

    # Result bpm and mood stored
    added_obj = mock_session.add.call_args[0][0]
    assert added_obj.bpm == 128.0
    assert added_obj.mood == "happy"
    assert result["status"] == "analyzed"


@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_process_file_retry(mock_pool: AsyncMock) -> None:
    """ANL-01+02: process_file raises exception when analysis fails; SAQ retries automatically."""
    mock_session = AsyncMock()

    file_record = _make_file_record()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = file_record
    mock_session.execute.return_value = mock_result

    mock_pool.side_effect = RuntimeError("process pool crashed")

    ctx = _make_ctx(mock_session=mock_session)
    with pytest.raises(RuntimeError, match="process pool crashed"):
        await process_file(ctx, file_id=str(uuid.uuid4()))
