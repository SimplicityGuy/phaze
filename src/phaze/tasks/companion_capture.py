"""Owning-agent capture task: killable bounded read, then authenticated immutable report."""

import asyncio
from datetime import UTC, datetime
import json
import sys
from typing import TYPE_CHECKING, Any
import uuid

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_companion_capture import CaptureCompanionPayload, CaptureReport
from phaze.tracklist_providers.domain import OutcomeStatus, SourceRead


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


async def capture_read(payload: CaptureCompanionPayload, scan_roots: list[str]) -> SourceRead:
    """Kill and reap on timeout AND cancellation; a blocked read cannot outlive this call."""
    request = json.dumps(
        {"target": payload.target.model_dump(mode="json"), "budget": payload.budget.model_dump(mode="json"), "scan_roots": scan_roots}
    ).encode()
    if len(request) > 32768:
        return SourceRead(status=OutcomeStatus.UNAVAILABLE, code="request_cap", scope=str(payload.target.file_id), retrieved_at=datetime.now(UTC))
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "phaze.services.companion_capture_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return SourceRead(
            status=OutcomeStatus.UNAVAILABLE, code="worker_start_failed", scope=str(payload.target.file_id), retrieved_at=datetime.now(UTC)
        )
    try:
        async with asyncio.timeout(payload.budget.deadline_seconds):
            output, _error = await process.communicate(request)
        if process.returncode != 0 or len(output) > payload.budget.max_wire_bytes:
            return SourceRead(
                status=OutcomeStatus.UNAVAILABLE, code="worker_failed", scope=str(payload.target.file_id), retrieved_at=datetime.now(UTC)
            )
        read = SourceRead.model_validate_json(output)
        CaptureReport(target=payload.target, budget=payload.budget, read=read)
        return read
    except TimeoutError:
        return SourceRead(status=OutcomeStatus.RETRY, code="deadline", scope=str(payload.target.file_id), retrieved_at=datetime.now(UTC))
    except ValueError:
        return SourceRead(
            status=OutcomeStatus.CONTRACT_ERROR, code="invalid_worker_output", scope=str(payload.target.file_id), retrieved_at=datetime.now(UTC)
        )
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def capture_companion_source(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Capture one expected source revision and report text independently of parsers/proposals."""
    payload = CaptureCompanionPayload.model_validate(kwargs)
    identity = ctx["agent_identity"]
    if identity.agent_id != payload.agent_id:
        raise ValueError("Capture payload agent does not match authenticated worker identity")
    cfg = get_settings()
    roots = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []
    read = await capture_read(payload, roots)
    api: PhazeAgentClient = ctx["api_client"]
    response = await api.post_companion_capture(CaptureReport(attempt_id=uuid.uuid4(), target=payload.target, budget=payload.budget, read=read))
    return {"observation_id": str(response.observation_id), "status": read.status.value, "freshness": response.freshness}
