"""SAQ task function for audio fingerprinting via fingerprint service containers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select

from phaze.models.file import FileRecord, FileState
from phaze.models.fingerprint import FingerprintResult


if TYPE_CHECKING:
    from phaze.services.fingerprint import FingerprintOrchestrator


logger = logging.getLogger(__name__)


async def fingerprint_file(ctx: dict[str, Any], *, file_id: str) -> dict[str, Any]:
    """Fingerprint a single file through all registered engines.

    Per D-17: Both engines always -- every file gets fingerprinted by both.
    Per D-18: File transitions to FINGERPRINTED after both engines succeed.
    Per D-16: Failed files marked with error, not transitioned.
    Retries with exponential backoff are handled by SAQ queue configuration.
    """
    async with ctx["async_session"]() as session:
        result = await session.execute(select(FileRecord).where(FileRecord.id == uuid.UUID(file_id)))
        file_record = result.scalar_one_or_none()
        if file_record is None:
            return {"file_id": file_id, "status": "not_found"}

        orchestrator: FingerprintOrchestrator = ctx["fingerprint_orchestrator"]
        results = await orchestrator.ingest_all(file_record.current_path)

        # Store per-engine results (upsert pattern per D-16)
        for engine_name, engine_result in results.items():
            existing = await session.execute(
                select(FingerprintResult).where(
                    FingerprintResult.file_id == file_record.id,
                    FingerprintResult.engine == engine_name,
                )
            )
            fprint = existing.scalar_one_or_none()
            if fprint is None:
                fprint = FingerprintResult(file_id=file_record.id, engine=engine_name)
                session.add(fprint)
            fprint.status = engine_result.status
            fprint.error_message = engine_result.error

        # Transition only if ALL engines succeeded (D-18)
        all_success = all(r.status == "success" for r in results.values())
        if all_success:
            file_record.state = FileState.FINGERPRINTED

        await session.commit()
        return {
            "file_id": file_id,
            "status": "fingerprinted" if all_success else "partial",
        }
