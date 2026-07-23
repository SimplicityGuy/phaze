"""Tests for the HTTP-rewritten extract_file_metadata task (Phase 26 Plan 11)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import ValidationError
import pytest

from phaze.services.metadata import ExtractedTags
from phaze.tasks.metadata_extraction import extract_file_metadata


def _make_ctx(api_client: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with an api_client mock."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.put_metadata = AsyncMock(return_value=MagicMock())
    return {"api_client": api_client}


def _make_payload_kwargs(file_id: uuid.UUID | None = None, file_type: str = "mp3") -> dict[str, Any]:
    return {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/track.mp3",
        "file_type": file_type,
        "agent_id": "test-agent",
    }


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_extract_calls_put_metadata(mock_extract: MagicMock) -> None:
    """Music file: tags extracted and posted via api.put_metadata."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    file_id = uuid.uuid4()

    mock_extract.return_value = ExtractedTags(
        artist="Test Artist",
        title="Test Title",
        album="Test Album",
        year=2024,
        genre="Electronic",
        track_number=3,
        duration=240.5,
        bitrate=320000,
        raw_tags={"TPE1": "Test Artist"},
    )

    result = await extract_file_metadata(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "extracted"
    assert result["file_id"] == str(file_id)
    mock_extract.assert_called_once_with("/music/track.mp3")
    api.put_metadata.assert_awaited_once()
    awaited_call = api.put_metadata.await_args
    assert awaited_call.args[0] == file_id
    body = awaited_call.args[1]
    assert body.artist == "Test Artist"
    assert body.title == "Test Title"
    assert body.duration == 240.5


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_companion_file_skipped(mock_extract: MagicMock) -> None:
    """Companion files (.txt) are skipped per D-10 -- no extract, no HTTP."""
    api = AsyncMock()
    api.put_metadata = AsyncMock()
    ctx = _make_ctx(api_client=api)

    result = await extract_file_metadata(ctx, **_make_payload_kwargs(file_type="txt"))

    assert result["status"] == "skipped"
    assert result["reason"] == "not_extractable"
    mock_extract.assert_not_called()
    api.put_metadata.assert_not_awaited()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_no_tags_still_posts_empty_metadata(mock_extract: MagicMock) -> None:
    """File with no tags still gets an empty metadata row (D-11)."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    mock_extract.return_value = ExtractedTags()  # all None

    result = await extract_file_metadata(ctx, **_make_payload_kwargs())

    assert result["status"] == "extracted"
    api.put_metadata.assert_awaited_once()
    body = api.put_metadata.await_args.args[1]
    assert body.artist is None
    assert body.title is None


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_extraction_failure_propagates(mock_extract: MagicMock) -> None:
    """Exception during tag extraction re-raises for SAQ retry handling."""
    api = AsyncMock()
    api.put_metadata = AsyncMock()
    ctx = _make_ctx(api_client=api)
    mock_extract.side_effect = RuntimeError("mutagen crashed")

    with pytest.raises(RuntimeError, match="mutagen crashed"):
        await extract_file_metadata(ctx, **_make_payload_kwargs())
    api.put_metadata.assert_not_awaited()


async def test_unreadable_file_fails_the_stage_not_an_empty_success() -> None:
    """phaze-todn: an I/O failure inside the REAL extract_tags must fail the stage.

    Previously extract_tags swallowed FileNotFoundError/OSError into an all-None
    ExtractedTags, so the task PUT an empty metadata row and returned
    status='extracted' -- the terminal report_metadata_failed + SAQ retry machinery
    built for exactly this case never fired. Uses the real extract_tags (no mock) on
    a nonexistent path to pin the end-to-end behavior: no put_metadata, terminal ack
    with the failure detail, then re-raise so SAQ records the failed attempt.
    """
    api = AsyncMock()
    api.put_metadata = AsyncMock()
    api.report_metadata_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = _job_stub(retryable=False)
    file_id = uuid.uuid4()

    kwargs = _make_payload_kwargs(file_id=file_id)
    kwargs["original_path"] = "/nonexistent/mount/track.mp3"

    with pytest.raises(FileNotFoundError):
        await extract_file_metadata(ctx, **kwargs)

    api.put_metadata.assert_not_awaited()
    api.report_metadata_failed.assert_awaited_once()
    failure = api.report_metadata_failed.await_args.args[1]
    assert failure.reason == "error"


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_rejects_extra_kwargs(mock_extract: MagicMock) -> None:
    """ExtractMetadataPayload.extra='forbid' rejects unknown fields."""
    api = AsyncMock()
    api.put_metadata = AsyncMock()
    ctx = _make_ctx(api_client=api)

    bad_kwargs = _make_payload_kwargs()
    bad_kwargs["bogus_field"] = "x"
    with pytest.raises(ValidationError):
        await extract_file_metadata(ctx, **bad_kwargs)
    mock_extract.assert_not_called()
    api.put_metadata.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 45 (L-02 / CR-02): terminal-failure ack discipline (mirrors process_file)
# ---------------------------------------------------------------------------


def _job_stub(*, retryable: bool) -> MagicMock:
    """A minimal SAQ Job stub exposing only the ``.retryable`` attribute the guard reads."""
    job = MagicMock()
    job.retryable = retryable
    return job


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_terminal_attempt_acks_then_raises(mock_extract: MagicMock) -> None:
    """Terminal attempt (job not retryable): report_metadata_failed called once, then re-raise."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(side_effect=RuntimeError("server down"))
    api.report_metadata_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = _job_stub(retryable=False)
    file_id = uuid.uuid4()
    mock_extract.return_value = ExtractedTags(artist="A")

    with pytest.raises(RuntimeError, match="server down"):
        await extract_file_metadata(ctx, **_make_payload_kwargs(file_id=file_id))

    # Phase 81 (FAIL-02): the terminal ack now carries a triage payload composed from the
    # original exception so control persists a durable metadata failure marker.
    from phaze.schemas.agent_metadata import MetadataFailurePayload

    api.report_metadata_failed.assert_awaited_once_with(file_id, MetadataFailurePayload(reason="error", error="server down"))


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_terminal_ack_failure_reraises_original_error(mock_extract: MagicMock) -> None:
    """WR-01: on the TERMINAL attempt, if report_metadata_failed ALSO raises (E2), the ORIGINAL
    task error (E1) must propagate -- not the ack error. The ack is awaited once, failure swallowed."""
    from phaze.services.agent_client import AgentApiServerError

    api = AsyncMock()
    api.put_metadata = AsyncMock(side_effect=RuntimeError("controller 5xx"))
    api.report_metadata_failed = AsyncMock(side_effect=AgentApiServerError("ack boom"))
    ctx = _make_ctx(api_client=api)
    ctx["job"] = _job_stub(retryable=False)
    file_id = uuid.uuid4()
    mock_extract.return_value = ExtractedTags(artist="A")

    # E1 (the put_metadata RuntimeError) propagates -- NOT E2 (the AgentApiServerError ack).
    with pytest.raises(RuntimeError, match="controller 5xx"):
        await extract_file_metadata(ctx, **_make_payload_kwargs(file_id=file_id))

    # Phase 81 (FAIL-02): the ack carries the triage payload composed from E1's message.
    from phaze.schemas.agent_metadata import MetadataFailurePayload

    api.report_metadata_failed.assert_awaited_once_with(file_id, MetadataFailurePayload(reason="error", error="controller 5xx"))


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_retryable_attempt_does_not_ack(mock_extract: MagicMock) -> None:
    """Retryable attempt: NO ack (row survives for the real retry), still re-raises."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(side_effect=RuntimeError("transient"))
    api.report_metadata_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = _job_stub(retryable=True)
    mock_extract.return_value = ExtractedTags(artist="A")

    with pytest.raises(RuntimeError, match="transient"):
        await extract_file_metadata(ctx, **_make_payload_kwargs())

    api.report_metadata_failed.assert_not_awaited()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_job_absent_does_not_ack(mock_extract: MagicMock) -> None:
    """No job in ctx (pure unit context): NO ack, still re-raises (mirrors `job is not None`)."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(side_effect=RuntimeError("boom"))
    api.report_metadata_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)  # no "job" key
    mock_extract.return_value = ExtractedTags(artist="A")

    with pytest.raises(RuntimeError, match="boom"):
        await extract_file_metadata(ctx, **_make_payload_kwargs())

    api.report_metadata_failed.assert_not_awaited()


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_success_path_does_not_ack(mock_extract: MagicMock) -> None:
    """Success path: report_metadata_failed is NOT called even on the terminal attempt."""
    api = AsyncMock()
    api.put_metadata = AsyncMock(return_value=MagicMock())
    api.report_metadata_failed = AsyncMock()
    ctx = _make_ctx(api_client=api)
    ctx["job"] = _job_stub(retryable=False)
    mock_extract.return_value = ExtractedTags(artist="A")

    result = await extract_file_metadata(ctx, **_make_payload_kwargs())

    assert result["status"] == "extracted"
    api.report_metadata_failed.assert_not_awaited()


# ---------------------------------------------------------------------------
# phaze-j8bj: the synchronous mutagen tag parse must run OFF the agent worker's
# event loop (via asyncio.to_thread) so a slow/hung media-mount read cannot freeze
# the loop the Phase-46 liveness heartbeat runs on.
# ---------------------------------------------------------------------------


@patch("phaze.tasks.metadata_extraction.extract_tags")
async def test_extract_tags_runs_off_loop(mock_extract: MagicMock) -> None:
    """extract_tags is dispatched through asyncio.to_thread, not called on the loop."""
    import asyncio

    api = AsyncMock()
    api.put_metadata = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)
    mock_extract.return_value = ExtractedTags(artist="A")

    real_to_thread = asyncio.to_thread
    offloaded: list[Any] = []

    async def _spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    with patch("phaze.tasks.metadata_extraction.asyncio.to_thread", side_effect=_spy):
        result = await extract_file_metadata(ctx, **_make_payload_kwargs())

    assert result["status"] == "extracted"
    # The exact extract_tags callable (the patched mock) must have been offloaded.
    assert mock_extract in offloaded
    mock_extract.assert_called_once_with("/music/track.mp3")


# NOTE (Phase 35 D-06): metadata extraction is operator-triggered ONLY. The former legacy
# ingestion auto-enqueue path was removed entirely in Phase 89 (LEGACY-01) along with
# ``services/ingestion.py``; the surviving agent-upsert inverse regression guard (an INSERT
# does NOT auto-enqueue) lives in tests/shared/core/test_no_auto_metadata_enqueue.py.
