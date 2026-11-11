"""Read model and in-memory preview construction for the CUE operator workspace."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, select
import structlog

from phaze.models.tracklist import Tracklist


if TYPE_CHECKING:
    from collections.abc import Callable
    import uuid

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


logger = structlog.get_logger(__name__)


class CueReviewReader(Protocol):
    """Consumer-facing port for CUE candidate counts and preview cards."""

    async def count_cue_review_candidates(self, session: AsyncSession, *, strict: bool = False) -> int: ...

    async def get_cue_review_cards(self, session: AsyncSession) -> list[dict[str, Any]]: ...


def _cue_card(
    tracklist: Tracklist,
    file_record: FileRecord,
    *,
    eligible: bool,
    build_error: bool = False,
    cue_text: str | None = None,
    version_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build the stable card shape shared by eligible, gated, and degraded rows."""
    return {
        "tracklist_id": tracklist.id,
        "file_id": file_record.id,
        "set_name": Path(file_record.current_path).stem,
        "eligible": eligible,
        "build_error": build_error,
        "cue_text": cue_text,
        "version_id": version_id,
    }


class SqlCueReviewReader:
    """SQLAlchemy-backed implementation of :class:`CueReviewReader`.

    Every legacy card is gated: a legacy tracklist never supplies a CUE offset, so it is listed for
    context only (docs/design/0027-retire-legacy-timestamp-qualification.md). A recording's CUE
    work goes through its reviewed selected source instead.
    """

    def __init__(self, *, max_rows: int, gated_tracklist_stmt: Callable[[], Select[Tracklist, FileRecord]]) -> None:
        self._max_rows = max_rows
        self._gated_tracklist_stmt = gated_tracklist_stmt

    async def count_cue_review_candidates(self, session: AsyncSession, *, strict: bool = False) -> int:
        try:
            async with session.begin_nested():
                gated = await session.execute(select(func.count()).select_from(self._gated_tracklist_stmt().subquery()))
                return int(gated.scalar_one())
        except Exception:
            if strict:
                raise
            logger.warning("cue_review_count_degraded", exc_info=True)
            return 0

    async def get_cue_review_cards(self, session: AsyncSession) -> list[dict[str, Any]]:
        try:
            async with session.begin_nested():
                gated_stmt = self._gated_tracklist_stmt().order_by(Tracklist.artist, Tracklist.event, Tracklist.id).limit(self._max_rows)
                return [
                    _cue_card(tracklist, file_record, eligible=False) for tracklist, file_record in (await session.execute(gated_stmt)).tuples().all()
                ]
        except Exception:
            logger.warning("cue_review_cards_degraded", exc_info=True)
            return []
