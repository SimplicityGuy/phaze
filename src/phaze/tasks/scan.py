"""SAQ task: scan_live_set -- fingerprint-query a live-set file, resolve tracklist, POST via HTTP (Phase 26 D-05, D-27).

Sends results to POST /api/internal/agent/tracklists with an agent-generated
request_id (UUID4) for Stripe-style idempotency. The same request_id must
be reused on SAQ retries so the server returns the cached response.

Per D-24: payload has NO current_path. The agent reads files via payload.original_path.
For artist/title resolution that previously joined FileMetadata in-process, the
agent now skips that join -- the controller (or a future enrichment task) can
resolve metadata after the fact via the tracklist_tracks rows. This is acceptable
because the v3.0 scan_live_set was the ONLY consumer of that join; future Phase
28's batch dispatch can perform the enrichment on the controller side.

W5 Option (b) per checker guidance: this is documented as a known v3.0 UI
regression for fingerprint-sourced tracklists -- artist/title will appear as
None in the tracklist review UI until controller-side enrichment lands.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/test_task_split.py (Plan 10).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
import uuid

from phaze.schemas.agent_tasks import ScanLiveSetPayload
from phaze.schemas.agent_tracklists import TracklistCreatePayload, TracklistTrackPayload


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient
    from phaze.services.fingerprint import FingerprintOrchestrator


logger = logging.getLogger(__name__)


async def scan_live_set(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Run fingerprint-query against a live-set file; POST tracklist via HTTP."""
    payload = ScanLiveSetPayload.model_validate(kwargs)

    api: PhazeAgentClient = ctx["api_client"]
    orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]

    matches = await orchestrator.combined_query(payload.original_path)
    if not matches:
        return {"file_id": str(payload.file_id), "status": "no_matches"}

    # Build the wire payload. Idempotency key = stable UUID per file_id so SAQ retries
    # of the same job collapse to one tracklist (server's Redis cache catches the replay).
    # Using uuid5 with payload.file_id + a phase namespace; predictable across retries.
    request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"phaze-scan-{payload.file_id}")
    external_id = f"fp-{payload.file_id.hex[:12]}"

    tracks = [
        TracklistTrackPayload(
            position=i + 1,
            artist=None,  # metadata-join skipped on agent; controller can enrich
            title=None,
            timestamp=match.timestamp,
            confidence=match.confidence,
        )
        for i, match in enumerate(matches)
    ]

    response = await api.create_tracklist(
        TracklistCreatePayload(
            file_id=payload.file_id,
            source="fingerprint",
            external_id=external_id,
            tracks=tracks,
            request_id=request_id,
        ),
    )

    return {
        "file_id": str(payload.file_id),
        "status": "scanned",
        "tracklist_id": str(response.tracklist_id),
        "version": response.version,
    }
