"""SAQ task: fingerprint_file -- submit a local file to audfprint + panako sidecars,
post per-engine result via HTTP (Phase 26 D-05).

Per D-17: both engines run on every file. Per D-18: file is considered fingerprinted
after both engines report success; state transition happens server-side via the
fingerprint endpoint's idempotent upsert and a future controller-side reducer
(Phase 27/28 will wire that). Plan 26-11 only sends the per-engine writes.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/test_task_split.py (Plan 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from phaze.schemas.agent_fingerprint import FingerprintWriteRequest
from phaze.schemas.agent_tasks import FingerprintFilePayload


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient
    from phaze.services.fingerprint import FingerprintOrchestrator


logger = structlog.get_logger(__name__)


async def fingerprint_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Fingerprint a file through both engines; PUT per-engine result via HTTP."""
    payload = FingerprintFilePayload.model_validate(kwargs)

    api: PhazeAgentClient = ctx["api_client"]
    orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]

    logger.info("fingerprint started", file_id=str(payload.file_id))

    # Submit to both audfprint + panako (local sidecars)
    results = await orchestrator.ingest_all(payload.original_path)

    # PUT per-engine result via HTTP -- idempotent on (file_id, engine) UQ
    all_success = True
    for engine_name, engine_result in results.items():
        body = FingerprintWriteRequest(
            status=engine_result.status,
            error_message=engine_result.error,
        )
        await api.put_fingerprint(payload.file_id, engine_name, body)
        logger.debug("fingerprint engine result", file_id=str(payload.file_id), engine=engine_name, status=engine_result.status)
        if engine_result.status != "success":
            all_success = False

    status = "fingerprinted" if all_success else "partial"
    logger.info("fingerprint completed", file_id=str(payload.file_id), status=status, engines=len(results))
    return {
        "file_id": str(payload.file_id),
        "status": status,
    }
