"""Tests for the HTTP-rewritten scan_live_set SAQ task (Phase 26 Plan 11)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from pydantic import ValidationError
import pytest

from phaze.services.fingerprint import CombinedMatch


def _make_ctx(api_client: AsyncMock | None = None, orchestrator: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with api_client + orchestrator mocks."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    if orchestrator is None:
        orchestrator = AsyncMock()
    return {"api_client": api_client, "fingerprint_orchestrator": orchestrator}


def _make_payload_kwargs(file_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/liveset.mp3",
        "agent_id": "test-agent",
    }


async def test_scan_no_matches_returns_no_matches() -> None:
    """When combined_query returns empty list, scan_live_set short-circuits."""
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=[])
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    file_id = uuid.uuid4()

    result = await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "no_matches"
    assert result["file_id"] == str(file_id)
    api.create_tracklist.assert_not_awaited()


async def test_scan_with_matches_posts_tracklist() -> None:
    """When matches exist, scan_live_set POSTs tracklist with stable uuid5 request_id."""
    from phaze.tasks.scan import scan_live_set

    matches = [
        CombinedMatch(track_id="track-1", confidence=85.0, timestamp="00:01:23"),
        CombinedMatch(track_id="track-2", confidence=70.0, timestamp="00:04:56"),
    ]
    api = AsyncMock()
    tracklist_id = uuid.uuid4()
    api.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=tracklist_id, version=1, track_count=2))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    file_id = uuid.uuid4()

    result = await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "scanned"
    assert result["tracklist_id"] == str(tracklist_id)
    assert result["version"] == 1

    api.create_tracklist.assert_awaited_once()
    body = api.create_tracklist.await_args.args[0]
    assert body.file_id == file_id
    assert body.source == "fingerprint"
    assert body.external_id == f"fp-{file_id.hex[:12]}"
    # request_id is a stable uuid5 of (NAMESPACE_URL, "phaze-scan-{file_id}")
    expected_request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"phaze-scan-{file_id}")
    assert body.request_id == expected_request_id
    assert len(body.tracks) == 2
    assert body.tracks[0].position == 1
    assert body.tracks[1].position == 2
    # Artist/title intentionally None per W5 Option (b) -- controller-side enrichment
    assert body.tracks[0].artist is None
    assert body.tracks[0].title is None


async def test_scan_request_id_is_stable_across_calls() -> None:
    """Re-running scan_live_set for the same file_id produces the same request_id."""
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api1 = AsyncMock()
    api1.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    orchestrator1 = AsyncMock()
    orchestrator1.combined_query = AsyncMock(return_value=matches)
    ctx1 = _make_ctx(api_client=api1, orchestrator=orchestrator1)

    api2 = AsyncMock()
    api2.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    orchestrator2 = AsyncMock()
    orchestrator2.combined_query = AsyncMock(return_value=matches)
    ctx2 = _make_ctx(api_client=api2, orchestrator=orchestrator2)

    file_id = uuid.uuid4()
    await scan_live_set(ctx1, **_make_payload_kwargs(file_id=file_id))
    await scan_live_set(ctx2, **_make_payload_kwargs(file_id=file_id))

    req_id_1 = api1.create_tracklist.await_args.args[0].request_id
    req_id_2 = api2.create_tracklist.await_args.args[0].request_id
    assert req_id_1 == req_id_2  # stable -- SAQ retries will hit cached response server-side


async def test_orchestrator_error_propagates() -> None:
    """Orchestrator failures propagate (SAQ retries)."""
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(side_effect=RuntimeError("audfprint down"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="audfprint down"):
        await scan_live_set(ctx, **_make_payload_kwargs())
    api.create_tracklist.assert_not_awaited()


async def test_http_error_propagates() -> None:
    """create_tracklist failures propagate (SAQ retries)."""
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api = AsyncMock()
    api.create_tracklist = AsyncMock(side_effect=RuntimeError("server is down"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="server is down"):
        await scan_live_set(ctx, **_make_payload_kwargs())


async def test_rejects_extra_kwargs() -> None:
    """ScanLiveSetPayload.extra='forbid' rejects unknown fields."""
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    orchestrator = AsyncMock()
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    bad_kwargs = _make_payload_kwargs()
    bad_kwargs["bogus_field"] = "x"
    with pytest.raises(ValidationError):
        await scan_live_set(ctx, **bad_kwargs)
    orchestrator.combined_query.assert_not_awaited()
    api.create_tracklist.assert_not_awaited()
