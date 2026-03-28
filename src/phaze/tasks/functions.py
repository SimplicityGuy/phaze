"""Task function definitions for arq workers."""

from __future__ import annotations

from typing import Any
import uuid

from arq import Retry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from phaze.config import settings
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.services.analysis import analyze_file
from phaze.tasks.pool import run_in_process_pool


_MUSIC_FILE_TYPES = frozenset({"mp3", "flac", "ogg", "m4a", "wav", "aiff", "wma", "aac", "opus"})


async def _get_session() -> AsyncSession:
    """Create a one-off async session for task use.

    Workers don't share the FastAPI app's engine. Each task creates
    its own lightweight session.
    """
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    session: AsyncSession = async_session()
    return session


async def process_file(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Process a single file through the audio analysis pipeline.

    Per D-05: one job per file, analysis via run_in_process_pool.
    Per D-03: retries with exponential backoff.
    """
    try:
        session = await _get_session()
        try:
            # 1. Fetch file record
            result = await session.execute(select(FileRecord).where(FileRecord.id == uuid.UUID(file_id)))
            file_record = result.scalar_one_or_none()
            if file_record is None:
                return {"file_id": file_id, "status": "not_found"}

            # 2. Skip non-music files
            if file_record.file_type not in _MUSIC_FILE_TYPES:
                return {"file_id": file_id, "status": "skipped", "reason": "not_music"}

            # 3. Run CPU-bound analysis in process pool
            analysis = await run_in_process_pool(ctx, analyze_file, file_record.current_path, settings.models_path)

            # 4. Upsert AnalysisResult
            existing = await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_record.id))
            analysis_result = existing.scalar_one_or_none()
            if analysis_result is None:
                analysis_result = AnalysisResult(file_id=file_record.id)
                session.add(analysis_result)

            analysis_result.bpm = analysis["bpm"]
            analysis_result.musical_key = analysis["musical_key"]
            analysis_result.mood = analysis["mood"]
            analysis_result.style = analysis["style"]
            analysis_result.features = analysis["features"]

            # 5. Update file state to ANALYZED
            file_record.state = FileState.ANALYZED

            await session.commit()
            return {"file_id": file_id, "status": "analyzed"}
        finally:
            await session.close()

    except Exception as exc:
        # Exponential backoff: 5s, 10s, 15s (job_try is 1-indexed)
        raise Retry(defer=ctx["job_try"] * 5) from exc
