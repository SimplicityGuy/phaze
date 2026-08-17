"""The per-stage PENDING sets -- the unbounded enqueue readers and their bounded render twins.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). The paging contract's
rule 7 split is the reason these live together: :func:`get_metadata_pending_files` /
:func:`get_discovered_files_with_duration` are UNBOUNDED BY DESIGN because they are the enqueue
membership, while :func:`get_pending_files_page` is the bounded RENDER read over the same predicate.
Keeping the pair adjacent is what stops a future edit "unifying" them and silently under-enqueuing
the backlog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, select
import structlog

from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.pagination import DEFAULT_PAGE_SIZE, Page, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.pipeline.common import _ACTIVE_CLOUD_STATUSES, MUSIC_VIDEO_TYPES
from phaze.services.stage_status import (
    dedup_resolved_clause,
    eligible_clause,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from phaze.routers.column_sort import SortState


logger = structlog.get_logger(__name__)


# --- Phase 49 duration-routing read helpers (D-05, D-09/D-10) ---------------------------
#
# The primitives the per-file router (Plan 02), backfill (Plan 03), and release cron
# (Plan 04) compose against. All three JOIN files -> metadata on FileMetadata.duration:
# FileRecord.file_metadata is lazy="noload" (models/file.py), so duration MUST be captured
# in-memory via an explicit SELECT before any background task reads it (a later lazy access
# off-session would raise). The backfill predicate filters ANALYSIS_FAILED *AND*
# duration >= threshold -- it deliberately does NOT reuse get_analysis_failed_count, which
# over-counts short/null-duration failures and would re-trigger the over-enqueue class.


async def get_discovered_files_with_duration(session: AsyncSession) -> list[tuple[FileRecord, float | None]]:
    """Return ``(FileRecord, duration)`` for every analyze-pending music/video file (LEFT OUTER JOIN metadata).

    READ-01 cutover: the analyze pending set is now DERIVED, not gated on ``FileRecord.state ==
    DISCOVERED``. A file is analyze-pending iff it is a music/video type, is ``eligible_clause(ANALYZE)``
    (``~inflight ∧ ~done ∧ ~failed`` -- ELIG-03 keeps a FAILED analyze terminal, the 44.5K over-enqueue
    guard), is NOT dedup-resolved, and is NOT being handled by the cloud path (T-82-A1). This dissolves
    the cross-stage deadlock the old state gate created -- a file whose ``state`` advanced past
    ``DISCOVERED`` (e.g. to ``METADATA_EXTRACTED``) but was never analyzed re-surfaces here correctly.

    The ``file_type.in_(MUSIC_VIDEO_TYPES)`` scope is NEWLY required: the old state-gated query was
    file-type-agnostic, so without it a non-music DISCOVERED file would leak into the analyze set
    (Pitfall 1). The ``~exists(cloud_job in ACTIVE statuses)`` conjunct is the explicit A1 double-dispatch
    guard -- see ``_ACTIVE_CLOUD_STATUSES``: a cloud-held/pushing file carries NO ``process_file`` ledger
    row, so ``eligible_clause``'s ``~inflight`` alone would re-admit it to the local analyze set.

    The duration is the joined ``FileMetadata.duration`` (or ``None`` when no metadata row exists yet).
    The LEFT OUTER JOIN is PRESERVED (the per-file cloud duration-router reads ``FileMetadata.duration``);
    it is captured into the in-memory list here because ``FileRecord.file_metadata`` is ``lazy="noload"``
    -- a later access in a background task would NOT lazy-load it.
    """
    stmt = (
        select(FileRecord, FileMetadata.duration)
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
        .where(
            FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
            eligible_clause(Stage.ANALYZE),
            ~dedup_resolved_clause(),
            ~exists(select(CloudJob.id).where(CloudJob.file_id == FileRecord.id, CloudJob.status.in_(_ACTIVE_CLOUD_STATUSES))),
        )
    )
    result = await session.execute(stmt)
    return [(record, duration) for record, duration in result.all()]


# --- Shared pending-set helpers (Phase 42, D-03 anti-drift) -----------------------------
#
# ONE definition of "pending" per stage, consumed by BOTH the Phase 39-41 manual DAG
# triggers (routers/pipeline.py) AND the Phase-42 recovery producer
# (tasks/reenqueue.recover_orphaned_work). Recovery and the manual triggers MUST read the
# SAME query so the two paths cannot drift apart (D-03): an identical pending set funnelled
# through the IDENTICAL keyed producer yields the IDENTICAL deterministic key, so a recovery
# re-enqueue dedups cleanly against any surviving in-flight job (no doubling, Phase-32 class).
# All queries are pure ORM / bound params -- NO f-string SQL (T-42-03).


async def get_metadata_pending_files(session: AsyncSession) -> list[FileRecord]:
    """Return the DERIVED metadata-extraction pending set -- music/video files eligible for metadata (READ-01).

    The EXACT set the manual metadata triggers (``trigger_metadata_extraction`` /
    ``trigger_extraction_ui``) and the Phase-42 recovery producer enqueue. READ-01 cutover: DERIVED from
    ``eligible_clause(METADATA)`` (``~inflight ∧ ~done`` -- ``ELIGIBLE_AFTER_FAILURE[METADATA]`` is True,
    so a FAILED metadata row stays eligible for the ELIG-04 auto-retry) instead of the prior
    state-agnostic "every music/video file", and excludes dedup-resolved files. A file whose metadata is
    genuinely done (a row present with ``failed_at`` NULL) drops out; a not-started or failed one stays.
    Pure ORM / bound params, NO interpolated operator input (T-42-03).
    UNBOUNDED BY DESIGN (paging contract rule 7, phaze.services.pagination). This is the ENQUEUE set
    -- the exact membership the bulk trigger and the recovery producer must schedule -- so it must
    NEVER be paged or LIMITed; doing so would silently under-enqueue the backlog. The WORKSPACE
    renders the bounded :func:`get_pending_files_page` instead. Keep the two readers separate.
    """
    stmt = select(FileRecord).where(
        FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),
        eligible_clause(Stage.METADATA),
        ~dedup_resolved_clause(),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _pending_page_stmt(stage: Stage, *, page: int, page_size: int, sort: SortState | None = None) -> Select[Any]:
    """Build the bounded pending-set page SELECT for an enrich workspace's pending set.

    The SAME membership predicate the unbounded enqueue reader uses
    (:func:`get_metadata_pending_files`), wrapped in the
    :mod:`phaze.services.pagination` contract: newest-first display order with the MANDATORY unique
    ``FileRecord.id`` tiebreaker (``created_at`` ties -- Postgres timestamp defaults are
    transaction-time constant), OFFSET paging, and a ``page_size + 1`` sentinel instead of a COUNT.
    """
    return paged_stmt(
        select(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES), eligible_clause(stage), ~dedup_resolved_clause()),
        page=page,
        page_size=page_size,
        # phaze-a6hm.1: the operator's whitelisted column when they picked one, else the newest-first
        # default. `sort` is a RESOLVED SortState, so this can only ever be an enumerated expression.
        order_by=sort.order_by() if sort is not None else (FileRecord.created_at.desc(),),
        tiebreaker=(FileRecord.id.desc(),),
    )


async def get_pending_files_page(
    session: AsyncSession, stage: Stage, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE, sort: SortState | None = None
) -> Page[FileRecord]:
    """Return ONE bounded page of ``stage``'s pending set -- the RENDER read for the enrich workspaces.

    phaze-5462: the metadata workspace used to render :func:`get_metadata_pending_files` in full,
    inline and UNBOUNDED -- exactly the cliff phaze-5462 fixed on the Analyze tab. It measured a
    harmless ~70 KB with zero rows only because that backlog happens to be EMPTY in production today;
    a metadata stall would have reproduced the 12.7 MB Analyze payload verbatim. This is the bounded
    read that surface renders instead.

    CRITICAL (paging contract rule 7): this is the RENDER read ONLY. The bulk EXTRACT ALL trigger
    keeps calling the UNBOUNDED ``get_metadata_pending_files`` reader, because enqueuing only the
    first page would silently under-enqueue the backlog -- a far worse bug than a long table. Do NOT
    "unify" the two readers.

    SAVEPOINT degrade-safe: returns an EMPTY page on any error rather than 500ing the workspace.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    try:
        async with session.begin_nested():
            raw = (await session.execute(_pending_page_stmt(stage, page=page, page_size=page_size, sort=sort))).scalars().all()
    except Exception:
        logger.warning("pending_files_page_degraded", stage=stage.value, page=page, page_size=page_size, exc_info=True)
        return Page(rows=[], page=page, page_size=page_size, has_next=False)
    rows, has_next = split_sentinel(raw, page_size)
    return Page(rows=rows, page=page, page_size=page_size, has_next=has_next)


async def get_metadata_failed_files(session: AsyncSession) -> list[FileRecord]:
    """Return every FileRecord carrying a terminal metadata failure row (FAIL-03 retry set).

    A metadata failure is persisted by the 81-03 writer as a ``metadata`` row with
    ``failed_at`` set and the payload columns NULL, so ``done(metadata)`` derives FAILED rather
    than DONE. This reuses the ``failed_clause(Stage.METADATA)`` shape (services/stage_status.py)
    -- a correlated ``exists(select(FileMetadata.id).where(file_id == FileRecord.id,
    FileMetadata.failed_at IS NOT NULL))`` -- so the operator bulk-retry endpoint re-enqueues
    EXACTLY the set the derivation reports as terminally failed. Pure ORM / bound params, NO
    f-string SQL (T-42-03).

    D-11: this returns the files; the retry LEAVES the failure row in place and re-enqueues --
    ``put_metadata``'s clear-on-success (81-03) wipes ``failed_at`` only when real metadata lands.
    """
    stmt = select(FileRecord).where(exists(select(FileMetadata.id).where(FileMetadata.file_id == FileRecord.id, FileMetadata.failed_at.isnot(None))))
    result = await session.execute(stmt)
    return list(result.scalars().all())
