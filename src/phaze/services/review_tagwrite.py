"""Bounded read model for the Tag-write operator workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from sqlalchemy import select, tuple_
from sqlalchemy.orm import selectinload
import structlog

from phaze.models.file import FileRecord
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services.stage_status import applied_clause


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    import uuid

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.discogs_link import DiscogsLink
    from phaze.models.tracklist import Tracklist


logger = structlog.get_logger(__name__)


class TagwriteReviewPage(NamedTuple):
    """The tag-write queue's bounded rows plus its honesty flag."""

    rows: list[dict[str, Any]]
    partial: bool


class TagwriteReviewReader(Protocol):
    """Consumer-facing port for the bounded Tag-write workspace read."""

    async def get_tagwrite_review_page(self, session: AsyncSession) -> TagwriteReviewPage: ...


class SqlTagwriteReviewReader:
    """SQLAlchemy implementation of :class:`TagwriteReviewReader`."""

    def __init__(
        self,
        *,
        max_rows: int,
        scan_batch: int,
        max_scan_batches: int,
        terminal_tagwrite_subq: Callable[[], Select[tuple[uuid.UUID]]],
        get_tracklists_for_files: Callable[[AsyncSession, list[uuid.UUID]], Awaitable[dict[uuid.UUID, Tracklist]]],
        get_accepted_discogs_links_for_files: Callable[[AsyncSession, dict[uuid.UUID, Tracklist]], Awaitable[dict[uuid.UUID, DiscogsLink]]],
        compute_proposed_tags: Callable[..., dict[str, Any]],
        build_comparison: Callable[..., list[dict[str, Any]]],
        count_changes: Callable[[list[dict[str, Any]]], int],
        summarize_tags: Callable[[list[dict[str, Any]], str], str],
        tag_review_payload: Callable[..., dict[str, Any]],
        encode_tag_review_token: Callable[[dict[str, Any]], str],
    ) -> None:
        self._max_rows = max_rows
        self._scan_batch = scan_batch
        self._max_scan_batches = max_scan_batches
        self._terminal_tagwrite_subq = terminal_tagwrite_subq
        self._get_tracklists_for_files = get_tracklists_for_files
        self._get_accepted_discogs_links_for_files = get_accepted_discogs_links_for_files
        self._compute_proposed_tags = compute_proposed_tags
        self._build_comparison = build_comparison
        self._count_changes = count_changes
        self._summarize_tags = summarize_tags
        self._tag_review_payload = tag_review_payload
        self._encode_tag_review_token = encode_tag_review_token

    async def _fetch_batch(
        self,
        session: AsyncSession,
        terminal_subq: Select[tuple[uuid.UUID]],
        last_key: tuple[str, Any] | None,
    ) -> list[FileRecord]:
        stmt = (
            select(FileRecord)
            .options(selectinload(FileRecord.file_metadata), selectinload(FileRecord.set_profile))
            .where(applied_clause(), FileRecord.id.not_in(terminal_subq))
            .order_by(FileRecord.original_filename, FileRecord.id)
            .limit(self._scan_batch)
        )
        if last_key is not None:
            stmt = stmt.where(tuple_(FileRecord.original_filename, FileRecord.id) > last_key)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def _fetch_batch_logs(session: AsyncSession, batch_ids: list[uuid.UUID]) -> dict[uuid.UUID, TagWriteLog]:
        log_rows = (
            (
                await session.execute(
                    select(TagWriteLog)
                    .where(TagWriteLog.file_id.in_(batch_ids))
                    .distinct(TagWriteLog.file_id)
                    .order_by(TagWriteLog.file_id, TagWriteLog.written_at.desc(), TagWriteLog.id.desc())
                )
            )
            .scalars()
            .all()
        )
        return {entry.file_id: entry for entry in log_rows}

    def _build_row(
        self,
        file_record: FileRecord,
        tracklist: Tracklist | None,
        discogs_link: DiscogsLink | None,
        latest_logs: dict[uuid.UUID, TagWriteLog],
    ) -> dict[str, Any] | None:
        proposed = self._compute_proposed_tags(
            file_record.file_metadata,
            tracklist,
            file_record.original_filename,
            discogs_link=discogs_link,
        )
        comparison = self._build_comparison(file_record.file_metadata, proposed)
        changed_count = self._count_changes(comparison)
        if changed_count < 1:
            return None
        latest = latest_logs.get(file_record.id)
        has_blanking = any(change["current"] is not None and change["proposed"] is None for change in comparison)
        return {
            "file_id": file_record.id,
            "filename": file_record.original_filename,
            "before_summary": self._summarize_tags(comparison, "current"),
            "after_summary": self._summarize_tags(comparison, "proposed"),
            "review_token": self._encode_tag_review_token(self._tag_review_payload(file_record, tracklist, discogs_link, proposed)),
            "changed_count": changed_count,
            "has_blanking": has_blanking,
            "has_prior_write": latest is not None,
            "latest_status": latest.status if latest is not None else None,
            "bulk_eligible": not has_blanking and (latest is None or (latest.source == "undo" and latest.status == TagWriteStatus.COMPLETED.value)),
            "status": ("blocked" if latest is not None and latest.status in {"failed", "discrepancy", "verify_failed"} else "needs_review"),
            "discrepancies": latest.discrepancies if latest is not None else None,
            "error_message": latest.error_message if latest is not None else None,
            "set_profile": file_record.set_profile,
        }

    def _rows_from_batch(
        self,
        batch: list[FileRecord],
        tracklists: dict[uuid.UUID, Tracklist],
        discogs_links: dict[uuid.UUID, DiscogsLink],
        latest_logs: dict[uuid.UUID, TagWriteLog],
        remaining_capacity: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, Any]] = []
        for index, file_record in enumerate(batch):
            row = self._build_row(file_record, tracklists.get(file_record.id), discogs_links.get(file_record.id), latest_logs)
            if row is None:
                continue
            rows.append(row)
            if len(rows) >= remaining_capacity:
                return rows, index < len(batch) - 1
        return rows, False

    async def get_tagwrite_review_page(self, session: AsyncSession) -> TagwriteReviewPage:
        try:
            async with session.begin_nested():
                terminal_subq = self._terminal_tagwrite_subq()
                rows: list[dict[str, Any]] = []
                partial = False
                last_key: tuple[str, Any] | None = None
                batches = 0
                while len(rows) < self._max_rows:
                    if batches >= self._max_scan_batches:
                        partial = True
                        break
                    batch = await self._fetch_batch(session, terminal_subq, last_key)
                    if not batch:
                        break
                    batches += 1
                    last_key = (batch[-1].original_filename, batch[-1].id)
                    batch_ids = [file_record.id for file_record in batch]
                    latest_logs = await self._fetch_batch_logs(session, batch_ids)
                    tracklists = await self._get_tracklists_for_files(session, batch_ids)
                    discogs_links = await self._get_accepted_discogs_links_for_files(session, tracklists)
                    new_rows, row_cap_hit_mid_batch = self._rows_from_batch(
                        batch,
                        tracklists,
                        discogs_links,
                        latest_logs,
                        self._max_rows - len(rows),
                    )
                    rows.extend(new_rows)
                    if len(rows) >= self._max_rows:
                        partial = row_cap_hit_mid_batch or len(batch) == self._scan_batch
                        break
                    if len(batch) < self._scan_batch:
                        break
                return TagwriteReviewPage(rows=rows, partial=partial)
        except Exception:
            logger.warning("tagwrite_review_rows_degraded", exc_info=True)
            return TagwriteReviewPage(rows=[], partial=False)
