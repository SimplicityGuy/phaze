"""Operational-emission tests (PR3 observability).

Asserts that the core operations emit their structured events at the EXPECTED
LEVELS (not exact strings): scans prove work at INFO with per-file DEBUG detail,
model validation announces at INFO, and the high-frequency heartbeat stays at
DEBUG so it never floods INFO. Also checks that a foreign stdlib record flows
through the configured root pipeline.

Events are captured with ``structlog.testing.capture_logs`` (event dicts incl.
``log_level``). The autouse ``_route_structlog_through_stdlib`` conftest fixture
configures structlog at DEBUG before each test, so DEBUG events are not filtered
out before the capture processor sees them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
import structlog
from structlog.testing import capture_logs


if TYPE_CHECKING:
    from pathlib import Path


def _events_at(captured: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    """Return every captured entry whose ``event`` matches ``event``."""
    return [entry for entry in captured if entry.get("event") == event]


@pytest.mark.asyncio
async def test_scan_directory_emits_started_completed_and_per_file_events(tmp_path: Path) -> None:
    """scan_directory emits 'scan started' + 'scan completed' at INFO and per-file at DEBUG."""
    import phaze.tasks.scan as scan

    (tmp_path / "track.mp3").write_bytes(b"\x00" * 32)

    api = AsyncMock()
    api.upsert_files = AsyncMock(return_value=MagicMock(upserted=1, inserted=1, enqueued=1))
    api.patch_scan_batch = AsyncMock(return_value=MagicMock())
    ctx: dict[str, Any] = {"api_client": api}

    with capture_logs() as captured:
        result = await scan.scan_directory(ctx, scan_path=str(tmp_path), batch_id=uuid.uuid4(), agent_id="test-agent")

    assert result["status"] == "completed"

    started = _events_at(captured, "scan started")
    completed = _events_at(captured, "scan completed")
    discovered = _events_at(captured, "file discovered")
    assert started and started[0]["log_level"] == "info"
    assert completed and completed[0]["log_level"] == "info"
    assert completed[0]["files"] == 1
    assert "duration_s" in completed[0]
    assert discovered and discovered[0]["log_level"] == "debug"


def test_ensure_models_present_emits_validating_at_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_models_present announces 'validating model weights' + 'models validated' at INFO."""
    import phaze.tasks._shared.model_bootstrap as mb

    monkeypatch.setattr(mb, "download_to", lambda _target: (10, 2))

    with capture_logs() as captured:
        mb.ensure_models_present(tmp_path)

    # "validating model weights" uses a %s template, so capture_logs records the raw
    # (un-interpolated) format string -- match by prefix rather than exact event.
    validating = [e for e in captured if str(e.get("event", "")).startswith("validating model weights")]
    validated = _events_at(captured, "models validated")
    assert validating and validating[0]["log_level"] == "info"
    assert validating[0]["count"] == mb._EXPECTED_MODEL_COUNT
    assert validated and validated[0]["log_level"] == "info"
    assert validated[0]["present_count"] == 10
    assert validated[0]["repaired_count"] == 2


@pytest.mark.asyncio
async def test_heartbeat_tick_emits_debug_not_info() -> None:
    """heartbeat_tick logs liveness at DEBUG and never at INFO (it fires every 30s)."""
    import phaze.tasks.heartbeat as heartbeat

    client = AsyncMock()
    client.heartbeat = AsyncMock()
    worker = MagicMock()
    worker.queue.info = AsyncMock(return_value={"queued": 0})
    ctx: dict[str, Any] = {
        "api_client": client,
        "agent_identity": MagicMock(agent_id="test-agent"),
        "worker": worker,
    }

    with capture_logs() as captured:
        await heartbeat.heartbeat_tick(ctx)

    sent = _events_at(captured, "heartbeat sent")
    assert sent and sent[0]["log_level"] == "debug"
    # The heartbeat path must NOT emit anything at INFO (anti-flood invariant).
    assert all(entry["log_level"] != "info" for entry in captured), captured


@pytest.mark.usefixtures("_route_structlog_through_stdlib")
def test_foreign_stdlib_log_flows_through_configured_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    """A foreign stdlib record renders through the configured root pipeline after configure_logging()."""
    import json

    from phaze.logging_config import configure_logging

    configure_logging(level="INFO", json_logs=True)
    logging.getLogger("uvicorn.error").info("foreign startup line")

    out = capsys.readouterr().out
    last = next(line for line in reversed(out.splitlines()) if line.strip())
    payload = json.loads(last)
    assert payload["event"] == "foreign startup line"
    assert payload["level"] == "info"
    assert "timestamp" in payload
    # Sanity: the captured structlog name should be the foreign logger.
    assert payload["logger"] == "uvicorn.error"
    structlog.reset_defaults()
