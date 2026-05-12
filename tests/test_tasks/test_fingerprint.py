"""Tests for the HTTP-rewritten fingerprint_file SAQ task (Phase 26 Plan 11)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from pydantic import ValidationError
import pytest


def _make_ctx(api_client: AsyncMock | None = None, orchestrator: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with api_client + orchestrator mocks."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.put_fingerprint = AsyncMock(return_value=MagicMock())
    if orchestrator is None:
        orchestrator = AsyncMock()
    return {"api_client": api_client, "fingerprint_orchestrator": orchestrator}


def _make_payload_kwargs(file_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/track.mp3",
        "agent_id": "test-agent",
    }


def _make_ingest_result(status: str = "success", error: str | None = None) -> MagicMock:
    """Create a mock IngestResult."""
    result = MagicMock()
    result.status = status
    result.error = error
    return result


async def test_both_engines_success_returns_fingerprinted() -> None:
    """fingerprint_file with both engines succeeding returns status=fingerprinted."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("success"),
            "panako": _make_ingest_result("success"),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    file_id = uuid.uuid4()

    result = await fingerprint_file(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "fingerprinted"
    assert result["file_id"] == str(file_id)
    orchestrator.ingest_all.assert_awaited_once_with("/music/track.mp3")
    # One PUT per engine
    assert api.put_fingerprint.await_count == 2
    engines_called = [call.args[1] for call in api.put_fingerprint.await_args_list]
    assert sorted(engines_called) == ["audfprint", "panako"]


async def test_one_engine_fails_returns_partial() -> None:
    """fingerprint_file with one engine failing returns status=partial."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(return_value=MagicMock())
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={
            "audfprint": _make_ingest_result("success"),
            "panako": _make_ingest_result("failed", error="HTTP 500"),
        },
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    result = await fingerprint_file(ctx, **_make_payload_kwargs())

    assert result["status"] == "partial"
    # Still PUT both engines (so server records the failure too)
    assert api.put_fingerprint.await_count == 2


async def test_http_error_propagates() -> None:
    """put_fingerprint failures propagate (SAQ will retry the job)."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock(side_effect=RuntimeError("server unreachable"))
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(
        return_value={"audfprint": _make_ingest_result("success")},
    )
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="server unreachable"):
        await fingerprint_file(ctx, **_make_payload_kwargs())


async def test_orchestrator_error_propagates() -> None:
    """Orchestrator failures propagate (SAQ retries)."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.ingest_all = AsyncMock(side_effect=RuntimeError("audfprint sidecar down"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="audfprint sidecar down"):
        await fingerprint_file(ctx, **_make_payload_kwargs())
    api.put_fingerprint.assert_not_awaited()


async def test_rejects_extra_kwargs() -> None:
    """FingerprintFilePayload.extra='forbid' rejects unknown fields."""
    from phaze.tasks.fingerprint import fingerprint_file

    api = AsyncMock()
    api.put_fingerprint = AsyncMock()
    orchestrator = AsyncMock()
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    bad_kwargs = _make_payload_kwargs()
    bad_kwargs["bogus_field"] = "x"
    with pytest.raises(ValidationError):
        await fingerprint_file(ctx, **bad_kwargs)
    orchestrator.ingest_all.assert_not_awaited()
    api.put_fingerprint.assert_not_awaited()
