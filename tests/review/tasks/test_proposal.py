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


def _make_analysis(file_id: uuid.UUID | None = None) -> MagicMock:
    """Create a mock AnalysisResult."""
    analysis = MagicMock()
    # phaze-vu88k.4: the batched read keys rows back to their file by `file_id`, so the mock has to
    # carry the real one -- an auto-MagicMock attribute would never match and the analysis would be
    # silently dropped from the built context.
    analysis.file_id = file_id
    analysis.bpm = 128.0
    analysis.musical_key = "Am"
    analysis.mood = "dark"
    analysis.style = "techno"
    analysis.features = {"energy": 0.85}
    return analysis


def _batch_query_results(
    file_records: list[MagicMock],
    analyses: list[MagicMock],
    metadata_rows: list[MagicMock],
) -> list[MagicMock]:
    """Stub the THREE batched reads ``generate_proposals`` issues in its read session, in order.

    phaze-vu88k.4: the task used to issue three round trips PER FILE, and these tests stubbed
    ``scalar_one_or_none`` three times per file. The reads are now three ``IN (...)`` queries for
    the whole batch -- files, then analysis, then metadata -- so the stubs return ``.scalars()``
    iterables that the task keys back by id. Same three reads in the same order against the same
    session; only the query shape changed.
    """
    results = []
    for rows in (file_records, analyses, metadata_rows):
        result = MagicMock()
        result.scalars.return_value = list(rows)
        results.append(result)
    return results


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
@patch("phaze.tasks.proposal.fetch_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_targets", new_callable=AsyncMock)
async def test_generate_proposals_happy_path(
    mock_targets: AsyncMock,
    mock_fetch: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """generate_proposals loads files, calls LLM, stores proposals, returns ok status."""
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)
    analysis = _make_analysis(file_id=file_id)

    session = AsyncMock()

    # The three batched reads, in order: FileRecord, AnalysisResult, FileMetadata (no metadata row).
    session.execute.side_effect = _batch_query_results([file_record], [analysis], [])

    mock_targets.return_value = {}
    mock_fetch.return_value = []

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
@patch("phaze.tasks.proposal.fetch_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_targets", new_callable=AsyncMock)
async def test_generate_proposals_file_not_found(
    _mock_targets: AsyncMock,
    _mock_fetch: AsyncMock,
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
    _mock_fetch.assert_not_called()


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
@patch("phaze.tasks.proposal.fetch_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_targets", new_callable=AsyncMock)
async def test_generate_proposals_calls_rate_limit(
    mock_targets: AsyncMock,
    mock_fetch: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """generate_proposals calls check_rate_limit with ctx["redis"] cache handle and settings max_rpm."""
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)
    analysis = _make_analysis(file_id=file_id)

    session = AsyncMock()

    session.execute.side_effect = _batch_query_results([file_record], [analysis], [])

    mock_targets.return_value = {}
    mock_fetch.return_value = []

    ctx = _make_ctx(mock_session=session)
    ctx["proposal_service"].generate_batch.return_value = SAMPLE_BATCH_RESPONSE
    mock_store.return_value = 1

    await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=0)

    mock_rate_limit.assert_called_once_with(ctx["redis"], 30)  # settings.llm_max_rpm default


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.fetch_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_targets", new_callable=AsyncMock)
async def test_generate_proposals_holds_no_session_across_rate_limit_and_llm(
    mock_targets: AsyncMock,
    mock_fetch: AsyncMock,
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

    phaze-potg5: the companion FETCH (the agent round-trip half of the old ``load_companion_contents``)
    is recorded here too and asserted to run after the read session closes and before the write
    session opens -- see ``test_generate_proposals_holds_no_session_across_companion_fetch`` below
    for a test isolating just that claim.
    """
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    events: list[str] = []

    def _make_recording_session() -> AsyncMock:
        s = AsyncMock()
        s.execute.side_effect = _batch_query_results([file_record], [], [])
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

    async def _companion_fetch_recording(*_a: Any, **_k: Any) -> list[dict[str, str]]:
        events.append("companion_fetch")
        return []

    async def _rate_limit_recording(*_a: Any, **_k: Any) -> None:
        events.append("rate_limit")

    async def _generate_recording(*_a: Any, **_k: Any) -> Any:
        events.append("llm")
        return SAMPLE_BATCH_RESPONSE

    async def _store_recording(*_a: Any, **_k: Any) -> int:
        events.append("store")
        return 1

    mock_targets.return_value = {"some-agent": []}
    mock_fetch.side_effect = _companion_fetch_recording
    mock_rate_limit.side_effect = _rate_limit_recording
    mock_store.side_effect = _store_recording

    ctx = _make_ctx()
    ctx["async_session"] = _factory
    ctx["proposal_service"].generate_batch.side_effect = _generate_recording

    result = await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=0)

    assert result["status"] == "ok"
    # Two distinct sessions were opened (read, then write) -- not one held across the whole task.
    assert session_count == 2
    # The read session closes BEFORE the companion fetch, the rate-limit backoff, and the LLM call.
    assert events.index("close1") < events.index("companion_fetch")
    assert events.index("close1") < events.index("rate_limit")
    assert events.index("close1") < events.index("llm")
    # The write session opens only AFTER every network await (companion fetch, rate limit, LLM).
    assert events.index("open2") > events.index("companion_fetch")
    assert events.index("open2") > events.index("rate_limit")
    assert events.index("open2") > events.index("llm")
    assert events.index("store") > events.index("open2")


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.fetch_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_targets", new_callable=AsyncMock)
async def test_generate_proposals_holds_no_session_across_companion_fetch(
    mock_targets: AsyncMock,
    mock_fetch: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """phaze-potg5: no DB session/transaction is held across the companion agent round-trip.

    Since phaze-6bkk, the per-file companion read is NOT a local read -- it is a
    ``queue.apply("read_companion_files", timeout=30, ...)`` request/response job dispatched to the
    owning agent's meta lane. Holding the read session open across that 30s-per-chunk network call
    (as the code did before this bead) pins a PgBouncer SESSION-mode connection idle-in-transaction
    for up to the full timeout, per file, per concurrent worker slot -- reproducing the pool
    exhaustion shape phaze-6fvu closed for the rate-limit/LLM calls. This test isolates just that
    claim: ``load_companion_targets`` (the DB read) must run INSIDE the read session, and
    ``fetch_companion_contents`` (the network round-trip) must run only AFTER that session has
    closed.
    """
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    file_record = _make_file_record(file_id=file_id)

    events: list[str] = []

    def _make_recording_session() -> AsyncMock:
        s = AsyncMock()
        s.execute.side_effect = _batch_query_results([file_record], [], [])
        return s

    def _factory() -> AsyncMock:
        cm = AsyncMock()

        async def _aenter(*_a: Any) -> AsyncMock:
            events.append("session_open")
            return _make_recording_session()

        async def _aexit(*_a: Any) -> bool:
            events.append("session_close")
            return False

        cm.__aenter__ = _aenter
        cm.__aexit__ = _aexit
        return cm

    async def _load_targets_recording(*_a: Any, **_k: Any) -> dict[str, list[Any]]:
        # The DB portion runs WHILE the read session is open -- record that it saw an open session
        # and no close yet.
        events.append("load_targets")
        return {"fileserver-01": []}

    async def _fetch_recording(*_a: Any, **_k: Any) -> list[dict[str, str]]:
        events.append("companion_fetch")
        return [{"filename": "info.nfo", "content": "hello"}]

    mock_targets.side_effect = _load_targets_recording
    mock_fetch.side_effect = _fetch_recording
    mock_rate_limit.return_value = None
    mock_store.return_value = 1

    ctx = _make_ctx()
    ctx["async_session"] = _factory
    ctx["proposal_service"].generate_batch.return_value = SAMPLE_BATCH_RESPONSE

    result = await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=0)

    assert result["status"] == "ok"
    # load_companion_targets (the DB read) happens BEFORE the read session closes.
    assert events.index("load_targets") < events.index("session_close")
    # fetch_companion_contents (the agent network round-trip) happens strictly AFTER the read
    # session closes -- this is the core regression this bead fixes.
    assert events.index("companion_fetch") > events.index("session_close")
    # And it is called with the targets load_companion_targets produced, not re-derived.
    mock_fetch.assert_awaited_once()
    fetch_call_kwargs = mock_fetch.await_args
    assert fetch_call_kwargs.args[0] == {"fileserver-01": []}


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


# ---------------------------------------------------------------------------
# phaze-02v1s: correlating a malformed completion with the files it cost
#
# `services/proposal.py` logs the MODE and a content preview -- it is the only layer holding the
# raw bytes. This layer is the only one holding the batch index and the file ids, and the seam
# finding named their absence explicitly ("no file ids, no model name, no content snippet, no way
# to tell which mode fired"). These two tests cover the half that lives here.
# ---------------------------------------------------------------------------


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.fetch_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_targets", new_callable=AsyncMock)
async def test_generate_proposals_logs_file_ids_when_the_completion_is_unparseable(
    mock_targets: AsyncMock,
    mock_fetch: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """The batch dies, and the log says WHICH files died with it and in which mode.

    The exception is re-raised unchanged: SAQ's retry and failure handling are untouched, and
    `MalformedCompletionError` is meant to fail the job loudly. This is logging only -- it is NOT
    the "zero exception handling" the finding described as absent being quietly filled in.
    """
    from structlog.testing import capture_logs

    from phaze.services.proposal import MalformedCompletionError
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    session = AsyncMock()
    session.execute.side_effect = _batch_query_results([_make_file_record(file_id=file_id)], [_make_analysis(file_id=file_id)], [])

    mock_targets.return_value = {}
    mock_fetch.return_value = []

    ctx = _make_ctx(mock_session=session)
    ctx["proposal_service"].generate_batch.side_effect = MalformedCompletionError("boom", mode="truncated")

    with capture_logs() as captured, pytest.raises(MalformedCompletionError):
        await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=7)

    event = next(item for item in captured if item.get("parse_mode") == "truncated")
    assert event["batch_index"] == 7
    assert event["file_ids"] == [str(file_id)]
    assert event["file_count"] == 1
    mock_store.assert_not_called()


@patch("phaze.tasks.proposal.store_proposals", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.fetch_companion_contents", new_callable=AsyncMock)
@patch("phaze.tasks.proposal.load_companion_targets", new_callable=AsyncMock)
async def test_generate_proposals_reports_a_salvaged_batch_and_still_stores_the_survivors(
    mock_targets: AsyncMock,
    mock_fetch: AsyncMock,
    mock_rate_limit: AsyncMock,
    mock_store: AsyncMock,
) -> None:
    """A salvaged batch proceeds -- the survivors are real proposals -- but it does not look clean.

    `store_proposals` is still called, because discarding a proposal is not a reason to discard the
    nine that were complete. What must not happen is the batch passing through indistinguishable
    from an untouched one.
    """
    from structlog.testing import capture_logs

    from phaze.services.proposal import SalvagedBatchProposalResponse
    from phaze.tasks.proposal import generate_proposals

    file_id = uuid.uuid4()
    session = AsyncMock()
    session.execute.side_effect = _batch_query_results([_make_file_record(file_id=file_id)], [_make_analysis(file_id=file_id)], [])

    mock_targets.return_value = {}
    mock_fetch.return_value = []

    ctx = _make_ctx(mock_session=session)
    ctx["proposal_service"].generate_batch.return_value = SalvagedBatchProposalResponse(
        proposals=[FileProposalResponse(file_index=0, proposed_filename="Kept.mp3", confidence=0.8, reasoning="complete")],
        discarded_positions=[1, 2],
    )
    mock_store.return_value = 1

    with capture_logs() as captured:
        result = await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=3)

    assert result["status"] == "ok"
    mock_store.assert_called_once()
    event = next(item for item in captured if "discarded_positions" in item)
    assert event["discarded_positions"] == [1, 2]
    assert event["kept"] == 1
    assert event["batch_index"] == 3
