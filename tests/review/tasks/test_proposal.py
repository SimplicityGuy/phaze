"""Tests for the proposal SAQ task function."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.services.proposal import BatchProposalResponse, FileProposalResponse


def _make_session_factory(mock_session: AsyncMock) -> MagicMock:
    """Create a mock async_sessionmaker that returns a context manager yielding mock_session."""
    factory = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = cm
    return factory


def _make_ctx(mock_session: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with mocked services."""
    if mock_session is None:
        mock_session = AsyncMock()
    mock_queue = MagicMock()
    # Phase 36: the broker is Postgres now -- generate_proposals rate-limits on the DEDICATED
    # cache-redis handle the control worker stashes at ctx["redis"], NOT ctx["queue"].redis.
    return {
        "queue": mock_queue,
        "redis": AsyncMock(),
        "proposal_service": AsyncMock(),
        "async_session": _make_session_factory(mock_session),
        "_mock_session": mock_session,
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
async def test_generate_proposals_happy_path(
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

    # First execute: FileRecord, Second: AnalysisResult, Third: FileMetadata
    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_analysis = MagicMock()
    mock_result_analysis.scalar_one_or_none.return_value = analysis
    mock_result_metadata = MagicMock()
    mock_result_metadata.scalar_one_or_none.return_value = None
    session.execute.side_effect = [mock_result_file, mock_result_analysis, mock_result_metadata]

    mock_companions.return_value = []

    ctx = _make_ctx(mock_session=session)
    ctx["proposal_service"].generate_batch.return_value = SAMPLE_BATCH_RESPONSE
    mock_store.return_value = 1

    result = await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=0)

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
async def test_generate_proposals_file_not_found(
    _mock_companions: AsyncMock,
    _mock_rate_limit: AsyncMock,
    _mock_store: AsyncMock,
) -> None:
    """generate_proposals returns empty status when no files found in DB."""
    from phaze.tasks.proposal import generate_proposals

    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    ctx = _make_ctx(mock_session=session)
    result = await generate_proposals(ctx, file_ids=[str(uuid.uuid4())], batch_index=0)

    assert result["status"] == "empty"
    assert result["count"] == 0
    _mock_rate_limit.assert_not_called()


async def test_generate_proposals_retry_on_exception() -> None:
    """generate_proposals re-raises exception for SAQ retry handling."""
    from phaze.tasks.proposal import generate_proposals

    session = AsyncMock()
    session.execute.side_effect = RuntimeError("DB connection failed")

    ctx = _make_ctx(mock_session=session)
    with pytest.raises(RuntimeError, match="DB connection failed"):
        await generate_proposals(ctx, file_ids=[str(uuid.uuid4())], batch_index=0)


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_contents", new_callable=AsyncMock)
async def test_generate_proposals_calls_rate_limit(
    mock_companions: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """generate_proposals calls check_rate_limit with ctx["redis"] cache handle and settings max_rpm."""
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)
    analysis = _make_analysis()

    session = AsyncMock()

    mock_result_file = MagicMock()
    mock_result_file.scalar_one_or_none.return_value = file_record
    mock_result_analysis = MagicMock()
    mock_result_analysis.scalar_one_or_none.return_value = analysis
    mock_result_metadata = MagicMock()
    mock_result_metadata.scalar_one_or_none.return_value = None
    session.execute.side_effect = [mock_result_file, mock_result_analysis, mock_result_metadata]

    mock_companions.return_value = []

    ctx = _make_ctx(mock_session=session)
    ctx["proposal_service"].generate_batch.return_value = SAMPLE_BATCH_RESPONSE
    mock_store.return_value = 1

    await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=0)

    mock_rate_limit.assert_called_once_with(ctx["redis"], 30)  # settings.llm_max_rpm default


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_contents", new_callable=AsyncMock)
async def test_generate_proposals_holds_no_session_across_rate_limit_and_llm(
    mock_companions: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """phaze-6fvu: no DB session is held across the rate-limit backoff or the LLM round-trip.

    Pre-6fvu a single session opened for the reads stayed open through check_rate_limit's
    asyncio.sleep loop and generate_batch's 30-120s LLM call, pinning a PgBouncer SESSION-mode
    connection idle-in-transaction; worker_max_jobs of these during a corpus drain drained the pool.
    The read session must CLOSE before those awaits and a FRESH session open only for the write. We
    record the session-lifecycle events interleaved with the rate-limit/LLM/store calls and assert the
    read session is exited before either network await, and the write session is opened after them.
    """
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    events: list[str] = []

    def _make_recording_session() -> AsyncMock:
        s = AsyncMock()
        mock_result_file = MagicMock()
        mock_result_file.scalar_one_or_none.return_value = file_record
        mock_result_none = MagicMock()
        mock_result_none.scalar_one_or_none.return_value = None
        s.execute.side_effect = [mock_result_file, mock_result_none, mock_result_none]
        return s

    session_count = 0

    def _factory() -> AsyncMock:
        nonlocal session_count
        session_count += 1
        idx = session_count
        cm = AsyncMock()

        async def _aenter(*_a: Any) -> AsyncMock:
            events.append(f"open{idx}")
            return _make_recording_session()

        async def _aexit(*_a: Any) -> bool:
            events.append(f"close{idx}")
            return False

        cm.__aenter__ = _aenter
        cm.__aexit__ = _aexit
        return cm

    async def _rate_limit_recording(*_a: Any, **_k: Any) -> None:
        events.append("rate_limit")

    async def _generate_recording(*_a: Any, **_k: Any) -> Any:
        events.append("llm")
        return SAMPLE_BATCH_RESPONSE

    async def _store_recording(*_a: Any, **_k: Any) -> int:
        events.append("store")
        return 1

    mock_companions.return_value = []
    mock_rate_limit.side_effect = _rate_limit_recording
    mock_store.side_effect = _store_recording

    ctx = _make_ctx()
    ctx["async_session"] = _factory
    ctx["proposal_service"].generate_batch.side_effect = _generate_recording

    result = await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=0)

    assert result["status"] == "ok"
    # Two distinct sessions were opened (read, then write) -- not one held across the whole task.
    assert session_count == 2
    # The read session closes BEFORE the rate-limit backoff and the LLM call.
    assert events.index("close1") < events.index("rate_limit")
    assert events.index("close1") < events.index("llm")
    # The write session opens only AFTER both network awaits complete.
    assert events.index("open2") > events.index("rate_limit")
    assert events.index("open2") > events.index("llm")
    assert events.index("store") > events.index("open2")


def test_controller_settings_contains_generate_proposals() -> None:
    """SAQ controller settings functions includes generate_proposals (Phase 26 D-03)."""
    from phaze.tasks.controller import settings as controller_settings

    func_names = [f.__name__ if callable(f) else str(f) for f in controller_settings["functions"]]
    assert "generate_proposals" in func_names


def test_controller_startup_creates_proposal_service() -> None:
    """startup function initializes proposal_service in context (Phase 26 D-03)."""
    # We verify by checking the startup function source references ProposalService
    import inspect

    from phaze.tasks.controller import startup

    source = inspect.getsource(startup)
    assert "proposal_service" in source
    assert "ProposalService" in source
