"""SAQ task function for AI proposal generation."""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select

from phaze.config import settings
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.proposal import (
    ProposalService,
    build_file_context,
    check_rate_limit,
    load_companion_contents,
    store_proposals,
)


async def generate_proposals(ctx: dict[str, Any], *, file_ids: list[str], batch_index: int) -> dict[str, Any]:
    """Generate AI filename proposals for a batch of files.

    Per D-14: Fixed-size batches.
    Per D-16: Runs through SAQ worker pool.
    Per D-19: Rate-limited via Redis counter.

    Args:
        ctx: SAQ job context (contains queue, proposal_service).
        file_ids: List of file UUID strings to process in this batch.
        batch_index: Index of this batch (for logging/tracking).

    Returns:
        Dict with batch index, count of proposals stored, and status.
    """
    async with ctx["async_session"]() as session:
        # 1. Build context for each file
        files_context: list[dict[str, Any]] = []
        valid_file_ids: list[str] = []
        for fid in file_ids:
            uid = uuid.UUID(fid)
            result = await session.execute(select(FileRecord).where(FileRecord.id == uid))
            file_record = result.scalar_one_or_none()
            if file_record is None:
                continue

            analysis_result_row = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == uid))
            analysis = analysis_result_row.scalar_one_or_none()

            metadata_row = await session.execute(select(FileMetadata).where(FileMetadata.file_id == uid))
            metadata = metadata_row.scalar_one_or_none()

            companions = await load_companion_contents(session, uid, settings.llm_max_companion_chars)

            ctx_dict = build_file_context(file_record, analysis, companions, metadata=metadata)
            ctx_dict["index"] = len(files_context)
            files_context.append(ctx_dict)
            valid_file_ids.append(fid)

        if not files_context:
            return {"batch": batch_index, "count": 0, "status": "empty"}

        # 2. Rate limit via Redis counter on the queue's Redis connection
        await check_rate_limit(ctx["queue"].redis, settings.llm_max_rpm)

        # 3. Call LLM
        proposal_service: ProposalService = ctx["proposal_service"]
        batch_response = await proposal_service.generate_batch(files_context)

        # 4. Store proposals
        stored = await store_proposals(session, valid_file_ids, batch_response, files_context)
        await session.commit()

        return {"batch": batch_index, "count": stored, "status": "ok"}
