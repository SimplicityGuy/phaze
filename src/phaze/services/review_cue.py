"""Read model and in-memory preview construction for the CUE operator workspace."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, select
import structlog

from phaze.models.tracklist import Tracklist


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    import uuid

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.services.cue_generator import CueTrackData


logger = structlog.get_logger(__name__)


class CueReviewReader(Protocol):
    """Consumer-facing port for CUE candidate counts and preview cards."""

    async def count_cue_review_candidates(self, session: AsyncSession) -> int: ...

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
    """SQLAlchemy-backed implementation of :class:`CueReviewReader`."""

    def __init__(
        self,
        *,
        max_rows: int,
        eligible_tracklist_stmt: Callable[[], Select[tuple[Tracklist, FileRecord]]],
        gated_tracklist_stmt: Callable[[], Select[tuple[Tracklist, FileRecord]]],
        get_eligible_tracklist_query: Callable[..., Awaitable[list[tuple[Tracklist, FileRecord]]]],
        build_cue_tracks_for_versions: Callable[[AsyncSession, Sequence[uuid.UUID]], Awaitable[dict[uuid.UUID, list[CueTrackData]]]],
        generate_cue_content: Callable[[str, str, list[CueTrackData]], str],
    ) -> None:
        self._max_rows = max_rows
        self._eligible_tracklist_stmt = eligible_tracklist_stmt
        self._gated_tracklist_stmt = gated_tracklist_stmt
        self._get_eligible_tracklist_query = get_eligible_tracklist_query
        self._build_cue_tracks_for_versions = build_cue_tracks_for_versions
        self._generate_cue_content = generate_cue_content

    async def count_cue_review_candidates(self, session: AsyncSession) -> int:
        try:
            async with session.begin_nested():
                eligible = await session.execute(select(func.count()).select_from(self._eligible_tracklist_stmt().subquery()))
                gated = await session.execute(select(func.count()).select_from(self._gated_tracklist_stmt().subquery()))
                return int(eligible.scalar_one()) + int(gated.scalar_one())
        except Exception:
            logger.warning("cue_review_count_degraded", exc_info=True)
            return 0

    def _build_eligible_card(
        self,
        tracklist: Tracklist,
        file_record: FileRecord,
        cue_tracks_by_version: dict[uuid.UUID, list[CueTrackData]],
    ) -> dict[str, Any]:
        try:
            cue_text: str | None = None
            if tracklist.latest_version_id:
                cue_tracks = cue_tracks_by_version.get(tracklist.latest_version_id, [])
                audio_name = Path(file_record.current_path).name
                cue_text = self._generate_cue_content(audio_name, file_record.file_type, cue_tracks)
        except Exception:
            logger.warning("cue_review_card_build_failed", tracklist_id=str(tracklist.id), exc_info=True)
            return _cue_card(tracklist, file_record, eligible=False, build_error=True)
        return _cue_card(
            tracklist,
            file_record,
            eligible=True,
            cue_text=cue_text,
            version_id=tracklist.latest_version_id,
        )

    async def _gated_cards(self, session: AsyncSession) -> list[dict[str, Any]]:
        gated_stmt = self._gated_tracklist_stmt().order_by(Tracklist.artist, Tracklist.event).limit(self._max_rows)
        return [_cue_card(tracklist, file_record, eligible=False) for tracklist, file_record in (await session.execute(gated_stmt)).tuples().all()]

    async def get_cue_review_cards(self, session: AsyncSession) -> list[dict[str, Any]]:
        try:
            async with session.begin_nested():
                eligible_pairs = (await self._get_eligible_tracklist_query(session, limit=self._max_rows))[: self._max_rows]
                version_ids = [tracklist.latest_version_id for tracklist, _ in eligible_pairs if tracklist.latest_version_id]
                cue_tracks_by_version = await self._build_cue_tracks_for_versions(session, version_ids)
                cards = [self._build_eligible_card(tracklist, file_record, cue_tracks_by_version) for tracklist, file_record in eligible_pairs]
                cards.extend(await self._gated_cards(session))
                return cards
        except Exception:
            logger.warning("cue_review_cards_degraded", exc_info=True)
            return []
