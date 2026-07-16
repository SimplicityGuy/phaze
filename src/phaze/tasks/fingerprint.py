"""SAQ task: fingerprint_file -- submit a local file to audfprint + panako sidecars,
post per-engine result via HTTP (Phase 26 D-05).

Per D-17: both engines run on every file. Per D-18/DERIV-05: the fingerprint stage
is DONE once ANY engine reports ``success``/``completed`` -- a failed sibling engine
does not block it. Completion is derived server-side (per-engine rows written by the
fingerprint endpoint's idempotent upsert, aggregated on read via ``done_clause`` /
``failed_clause``) rather than by flipping a single file-level state. This task only
sends the per-engine writes.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10).
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

    try:
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
    except Exception:
        # Phase 45 (L-02 / CR-02): clear the single-per-file scheduling-ledger row on the TERMINAL
        # attempt only (a failure anywhere in the orchestrator submit or the per-engine PUT loop),
        # then re-raise so SAQ records the failed attempt. A retryable attempt (or job absent in a
        # pure unit test) re-raises silently so the one real retry can run -- the row survives for
        # it (T-45-06). Mirrors process_file's generic guard (functions.py:179-189). Without this
        # ack a terminally-failed fingerprint file stays in get_fingerprint_pending_files forever,
        # so is_domain_completed can never fire and recover_orphaned_work re-enqueues it every pass.
        job = ctx.get("job")
        if job is not None and not job.retryable:
            # Best-effort ack: if report_fingerprint_failed ALSO raises (E2) while handling the
            # original failure (E1), swallow + log E2 so the bare `raise` below always re-raises
            # E1 -- SAQ must record the real task error, not the ack error (WR-01).
            try:
                await api.report_fingerprint_failed(payload.file_id)
            except Exception:
                logger.warning("fingerprint_file terminal-ack failed", file_id=str(payload.file_id), exc_info=True)
        raise

    status = "fingerprinted" if all_success else "partial"
    logger.info("fingerprint completed", file_id=str(payload.file_id), status=status, engines=len(results))
    return {
        "file_id": str(payload.file_id),
        "status": status,
    }
