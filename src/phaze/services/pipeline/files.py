"""The scannable per-file overview -- the bounded, per-row-derived files page plus the
single-file stage matrix and orphan diagnostics behind the record slide-in.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import false, select
import structlog

from phaze.enums.stage import Stage, Status
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.services.pagination import DEFAULT_PAGE_SIZE, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.pipeline.buckets import ORPHANED_BUCKET
from phaze.services.stage_status import (
    orphaned_clause,
    stage_status_case,
)


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from phaze.routers.column_sort import SortState


logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------------------------------
# Phase 87 (87-04, UI-01 / D-02 / PERF-01): the scannable, per-row-derived files page.
#
# The operator's "where's this file at?" overview. Two anti-features are forbidden by the phase's
# anti-feature table and BOTH are honoured here: (1) "rendering raw internal status strings" -- every
# per-stage cell is the DERIVED stage_status_case bucket, never FileRecord.state; (2) "a stats poll
# that scans the whole corpus" -- the query is LIMIT-bounded, keyset/offset-paginated, and NEVER emits
# an unbounded whole-corpus COUNT (the +1 sentinel below computes has_next instead). The five correlated
# stage_status_case CASE columns evaluate for the N page rows ONLY (they correlate to FileRecord), so
# the per-page derivation cost is O(page_size), never O(corpus) -- the T-87-11 DoS mitigation.
# --------------------------------------------------------------------------------------------------

# The five pills the UI shows, in matrix order. The 6-stage -> 5-pill remap LANDMINE lives HERE and in
# _stage_matrix.html: tracklist is omitted; Appr = REVIEW, Exec = APPLY. `.value` keys the row dict so
# the template reads buckets.review for the Appr pill and buckets.apply for the Exec pill.
_FILES_PAGE_STAGES: tuple[Stage, ...] = (
    Stage.METADATA,
    Stage.ANALYZE,
    Stage.PROPOSE,
    Stage.REVIEW,
    Stage.APPLY,
)


@dataclass
class FilesPageRow:
    """One rendered file row: the ORM record + its five DERIVED per-stage buckets (keyed by Stage value)."""

    file: FileRecord
    buckets: dict[str, str]


@dataclass
class FilesPage:
    """A bounded, derive-per-row page of files. ``has_next`` comes from a +1 sentinel row -- never a COUNT."""

    rows: list[FilesPageRow] = field(default_factory=list)
    page: int = 1
    # Contract rule 3: the page size is owned by phaze.services.pagination, never re-spelled here.
    page_size: int = DEFAULT_PAGE_SIZE
    has_next: bool = False


def _files_page_stmt(*, page: int, page_size: int, stage: Stage | None, bucket: str | None, sort: SortState | None = None) -> Select[Any]:
    """Build the bounded per-page derivation SELECT (extracted so the EXPLAIN test can probe it directly).

    ``select(FileRecord, stage_status_case(METADATA), ... , stage_status_case(APPLY))`` ordered by
    ``sort`` (phaze-a6hm.3) -- or, absent a resolved sort, the ``FileRecord.id`` PK index -- and LIMITed
    to ``page_size + 1`` (the sentinel that yields ``has_next`` with NO COUNT). Each ``stage_status_case``
    is a correlated CASE over the Phase-77 partial indexes (``ix_metadata_failed`` / ``ix_analysis_completed``
    / ``ix_analysis_failed``), so the derivation touches only the page rows. The
    optional ``stage``+``bucket`` filter is applied as ``stage_status_case(stage) == bucket`` -- a pure
    ORM bound-param comparison (never f-string SQL, T-87-14); the caller validates ``stage``/``bucket``
    against the ``Stage``/``Status`` allowlists (plus :data:`ORPHANED_BUCKET`, below).

    phaze-cavai (the orphaned lens): ``bucket == ORPHANED_BUCKET`` is NOT a sixth per-row CASE arm --
    D-01a deliberately keeps ``saq_jobs`` out of the hot per-row derivation, so the matrix pills stay
    five-bucket. The lens instead narrows the ``in_flight`` rows through :func:`orphaned_clause`
    (the per-file twin of recovery's work set) as a WHERE-only refinement: the unfiltered poll pays
    nothing, and the filtered listing is exactly the stage's recovery candidate set.
    ``orphaned_clause`` is defined only for the two enrich stages (``domain_completed_clause`` raises
    otherwise), so any other stage yields the empty set via ``false()`` rather than a 500 into the
    SAVEPOINT degrade path -- a propose/review/apply orphan is not a defined concept.
    """
    cols = [stage_status_case(s) for s in _FILES_PAGE_STAGES]
    stmt = select(FileRecord, *cols)
    if stage is not None and bucket is not None:
        if bucket == ORPHANED_BUCKET:
            if stage in (Stage.METADATA, Stage.ANALYZE):
                stmt = stmt.where(stage_status_case(stage) == Status.IN_FLIGHT.value, orphaned_clause(stage))
            else:
                stmt = stmt.where(false())
        else:
            stmt = stmt.where(stage_status_case(stage) == bucket)
    # The paging contract (phaze.services.pagination): OFFSET + a page_size+1 sentinel for has_next
    # (never a whole-corpus COUNT -- T-87-11). FileRecord.id is the mandatory unique tiebreaker
    # (paging contract rule 4) regardless of `sort` -- an operator-chosen column ties far more often
    # than the PK does (column_sort contract, SortState.order_by docstring).
    return paged_stmt(
        stmt,
        page=page,
        page_size=page_size,
        order_by=sort.order_by() if sort is not None else (),
        tiebreaker=(FileRecord.id,),
    )


async def get_files_page(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    stage: Stage | None = None,
    bucket: str | None = None,
    sort: SortState | None = None,
) -> FilesPage:
    """Return one bounded, per-row-derived page of files -- SAVEPOINT degrade-safe, never a whole-corpus scan.

    Clamps ``page``/``page_size`` via the :mod:`phaze.services.pagination` contract, builds the bounded :func:`_files_page_stmt`, and
    runs it inside a ``begin_nested()`` SAVEPOINT so ANY error (a DB hiccup, an aborted transaction, a
    build-time raise) rolls back the nested scope ALONE, logs a warning, and returns a safe EMPTY page --
    it NEVER 500s the poll (INFLIGHT-02 / D-00c / T-87-12). ``has_next`` is derived from the LIMIT+1
    sentinel row, so pagination costs no COUNT. The five correlated ``stage_status_case`` columns are read
    back into each row's ``buckets`` dict keyed by ``Stage`` value (metadata/analyze/propose/review/apply) -- the derived buckets the ``_stage_pill`` cells render (never ``FileRecord.state``).

    ``stage``+``bucket`` are accepted NOW (plumbed straight through to the filter) so Plan 05 -- which
    wires the status filter bar -- is templates-only. Passing only one of the pair is a no-op filter.

    ``sort`` (phaze-a6hm.3) is an already-resolved :class:`~phaze.routers.column_sort.SortState` from
    the router's ``FILES_SORT`` contract -- this layer never sees the raw wire ``sort``/``order``
    strings, only the whitelisted expression :meth:`~phaze.routers.column_sort.SortState.order_by`
    hands back. ``None`` (e.g. a caller that predates phaze-a6hm.3) falls back to the original
    ``FileRecord.id`` order.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    try:
        async with session.begin_nested():
            stmt = _files_page_stmt(page=page, page_size=page_size, stage=stage, bucket=bucket, sort=sort)
            result = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("files_page_degraded", page=page, page_size=page_size, exc_info=True)
        return FilesPage(rows=[], page=page, page_size=page_size, has_next=False)
    page_rows, has_next = split_sentinel(result, page_size)
    rows = [
        FilesPageRow(
            file=row[0],
            buckets={stage_member.value: row[idx + 1] for idx, stage_member in enumerate(_FILES_PAGE_STAGES)},
        )
        for row in page_rows
    ]
    return FilesPage(rows=rows, page=page, page_size=page_size, has_next=has_next)


async def get_file_stage_buckets(session: AsyncSession, file_id: uuid.UUID) -> dict[str, str]:
    """Return ONE file's six derived per-stage buckets (keyed by ``Stage`` value) — the matrix row, single-file.

    The record slide-in's Stage-Eligibility pills must show the SAME derived status the Files matrix
    renders for that file (CONSOLE-01: one status source, no divergent second derivation), so this is
    the same six correlated ``stage_status_case`` CASE columns as :func:`_files_page_stmt`, scoped to
    a single ``FileRecord.id`` — an O(1) single-row read, never a corpus scan. Degrades to an
    all-``not_started`` mapping on any error (the pane renders, never 500s) — mirroring
    :func:`get_files_page`'s SAVEPOINT degrade posture.
    """
    cols = [stage_status_case(s) for s in _FILES_PAGE_STAGES]
    try:
        async with session.begin_nested():
            row = (await session.execute(select(*cols).where(FileRecord.id == file_id))).one_or_none()
    except Exception:
        logger.warning("file_stage_buckets_degraded", file_id=str(file_id), exc_info=True)
        row = None
    if row is None:
        return dict.fromkeys((s.value for s in _FILES_PAGE_STAGES), "not_started")
    return {stage_member.value: row[idx] for idx, stage_member in enumerate(_FILES_PAGE_STAGES)}


# phaze-cavai: the (stage -> ledger function) pairs orphan diagnostics can explain. Exactly the two
# enrich stages orphaned_clause is defined for; the key format is the deterministic-key contract's
# "<function>:<natural_id>" with natural_id == file_id for both.
_ORPHAN_DETAIL_STAGES: tuple[tuple[Stage, str], ...] = ((Stage.METADATA, "extract_file_metadata"), (Stage.ANALYZE, "process_file"))


async def get_file_orphan_details(session: AsyncSession, file_id: uuid.UUID) -> dict[str, dict[str, Any] | None]:
    """Return ONE file's per-enrich-stage orphan diagnostics, or ``None`` per non-orphaned stage (phaze-cavai).

    Evaluates :func:`~phaze.services.stage_status.orphaned_clause` single-file — the SAME predicate the
    Files orphaned lens filters on and recovery re-drives, so this pane can never disagree with either —
    and, for an orphaned stage, reads the ``scheduling_ledger`` facts that explain the strand: when it
    was scheduled (``enqueued_at``), and the ``timeout`` / ``retries`` / ``redrive_attempt`` replay
    budget captured at enqueue time. The record pane pairs this with its own already-loaded
    started-vs-never-started evidence (partial analysis / windows), which this read does not duplicate.

    Degrade-safe (mirrors :func:`get_file_stage_buckets`): ANY error rolls back the SAVEPOINT alone and
    returns the all-``None`` mapping — the record pane renders without diagnostics, never a 500.
    """
    out: dict[str, dict[str, Any] | None] = {stage_member.value: None for stage_member, _fn in _ORPHAN_DETAIL_STAGES}
    try:
        async with session.begin_nested():
            flags = (
                await session.execute(
                    select(*[orphaned_clause(stage_member) for stage_member, _fn in _ORPHAN_DETAIL_STAGES]).where(FileRecord.id == file_id)
                )
            ).one_or_none()
            if flags is None:
                return out
            for idx, (stage_member, function) in enumerate(_ORPHAN_DETAIL_STAGES):
                if not flags[idx]:
                    continue
                ledger = (await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"{function}:{file_id}"))).scalar_one_or_none()
                out[stage_member.value] = {
                    "enqueued_at": ledger.enqueued_at if ledger is not None else None,
                    "timeout": ledger.timeout if ledger is not None else None,
                    "retries": ledger.retries if ledger is not None else None,
                    "redrive_attempt": ledger.redrive_attempt if ledger is not None else None,
                }
    except Exception:
        logger.warning("file_orphan_details_degraded", file_id=str(file_id), exc_info=True)
        return {stage_member.value: None for stage_member, _fn in _ORPHAN_DETAIL_STAGES}
    return out
