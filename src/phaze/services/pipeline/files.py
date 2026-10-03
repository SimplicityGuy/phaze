"""The scannable per-file overview -- the bounded, per-row-derived files page plus the
single-file stage matrix and orphan diagnostics behind the record slide-in.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import selectinload
import structlog

from phaze.enums.stage import Stage, Status
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist_lookup_cache import TracklistFileLookup
from phaze.services.pagination import DEFAULT_PAGE_SIZE, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.pipeline.buckets import ORPHANED_BUCKET
from phaze.services.stage_status import (
    orphaned_clause,
    stage_status_case,
)


if TYPE_CHECKING:
    import uuid

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from phaze.routers.column_sort import SortState


logger = structlog.get_logger(__name__)


# The scannable, per-row-derived files page (UI-01 / D-02 / PERF-01).
#
# The operator's "where's this file at?" overview. Two anti-features are forbidden by the phase's
# anti-feature table and BOTH are honoured here: (1) "rendering raw internal status strings" -- every
# per-stage cell is the DERIVED stage_status_case bucket, never FileRecord.state; (2) "a stats poll
# that scans the whole corpus" -- the query is LIMIT-bounded, keyset/offset-paginated, and NEVER emits
# an unbounded whole-corpus COUNT (the +1 sentinel below computes has_next instead). The five correlated
# stage_status_case CASE columns evaluate for the N page rows ONLY (they correlate to FileRecord), so
# the per-page derivation cost is O(page_size), never O(corpus) -- the T-87-11 DoS mitigation.

# The stage columns the Files table shows, in column order. `.value` keys the row dict so the template
# reads buckets.review for the "Review" column and buckets.apply for "Execute". phaze-o71bf added
# TRACKLIST (it was omitted while its status could only say "has a row or not"); the five-pill
# _stage_matrix.html and _files_current_status.html still name their own five stages explicitly, so
# the extra key here changes neither.
#
# TRACKLIST is therefore also a disjunct of the "any stage = <status>" OR below (phaze-7sdwt) -- an
# implementer's decision, not an operator one: the OR means "some stage column on this page shows that
# status", and a Tracklist column that could show "failed" while "any stage = failed" hid the row would
# make the filter disagree with the table it filters.
_FILES_PAGE_STAGES: tuple[Stage, ...] = (
    Stage.METADATA,
    Stage.ANALYZE,
    Stage.TRACKLIST,
    Stage.PROPOSE,
    Stage.REVIEW,
    Stage.APPLY,
)

# phaze-7sdwt: the stages ORPHANED_BUCKET is a defined concept for -- the same two enrich stages
# orphaned_clause raises on any other stage for (see its docstring). "any stage = orphaned" is an OR
# over exactly this pair, never all of _FILES_PAGE_STAGES (propose/review/apply orphaned is not a
# defined concept, so it must not silently join the OR as an always-false no-op disjunct).
_ORPHAN_ELIGIBLE_STAGES: tuple[Stage, ...] = (Stage.METADATA, Stage.ANALYZE)


def _orphan_row_clause(stage: Stage) -> ColumnElement[bool]:
    """One stage's row-level orphaned predicate: in-flight per the CASE ladder AND orphaned_clause.

    Mirrors ``_files_page_stmt``'s existing single-stage orphaned branch exactly (the stage_status_case
    ``== IN_FLIGHT`` conjunct plus ``orphaned_clause``), extracted so both the single-stage and the
    any-stage (OR) lenses share ONE spelling of the per-stage orphaned test.
    """
    return and_(stage_status_case(stage) == Status.IN_FLIGHT.value, orphaned_clause(stage))


@dataclass
class FilesPageRow:
    """One rendered file row: the ORM record + its DERIVED per-stage buckets (keyed by Stage value).

    ``tracklist_lookup`` is the file's ``tracklist_file_lookups`` row (phaze-o71bf), or ``None`` when
    no drain slice has seen it -- the detail (outcome, retry date) the Tracklist pill needs beyond
    the bucket.
    """

    file: FileRecord
    buckets: dict[str, str]
    tracklist_lookup: TracklistFileLookup | None = None


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
    is a correlated CASE over the stage-status partial indexes (``ix_metadata_failed`` / ``ix_analysis_completed``
    / ``ix_analysis_failed``), so the derivation touches only the page rows. The
    optional ``stage``+``bucket`` filter is applied as ``stage_status_case(stage) == bucket`` -- a pure
    ORM bound-param comparison (never f-string SQL, T-87-14); the caller validates ``stage``/``bucket``
    against the ``Stage``/``Status`` allowlists (plus :data:`ORPHANED_BUCKET`, below).

    phaze-7sdwt (operator report 2026-09-27): ``bucket`` alone -- "any stage = <status>" -- is a REAL
    filter, implemented as "some stage has that status": an OR of ``stage_status_case(s) == bucket``
    across every :data:`_FILES_PAGE_STAGES` member. Before this it was silently dropped (only a
    ``stage``+``bucket`` PAIR filtered anything), so picking a status with "any stage" returned the
    whole unfiltered corpus while the UI still looked filtered (a pushed URL, a "Clear filter"
    control, the filtered empty-state copy). The REVERSE case -- ``stage`` alone, "any status" -- stays
    a deliberate no-op (there is no status to compare against), so the caller must not report that a
    filter applied from ``stage`` alone; ``bucket is not None`` is the sole signal that a filter ran.

    phaze-cavai (the orphaned lens): ``bucket == ORPHANED_BUCKET`` is NOT a sixth per-row CASE arm --
    D-01a deliberately keeps ``saq_jobs`` out of the hot per-row derivation, so the matrix pills stay
    five-bucket. The lens instead narrows the ``in_flight`` rows through :func:`orphaned_clause`
    (the per-file twin of recovery's work set) as a WHERE-only refinement: the unfiltered poll pays
    nothing, and the filtered listing is exactly the stage's recovery candidate set.
    ``orphaned_clause`` is defined only for the two enrich stages (``domain_completed_clause`` raises
    otherwise), so any other stage -- or "any stage" itself -- narrows to exactly
    :data:`_ORPHAN_ELIGIBLE_STAGES` (METADATA + ANALYZE), never all five: a propose/review/apply
    orphan is not a defined concept, so those three must not join the OR as always-false disjuncts,
    and a single ineligible stage yields the empty set via ``false()`` rather than a 500 into the
    SAVEPOINT degrade path.
    """
    cols = [stage_status_case(s) for s in _FILES_PAGE_STAGES]
    # phaze-x1qr3.9: ONE extra `SELECT ... WHERE set_profile.file_id IN (...)` for the whole
    # bounded page (selectinload, not a join on the correlated derivation above) -- never one
    # query per row. `FileRecord.set_profile` is `lazy="noload"` (models/file.py), so without this
    # every `row.file.set_profile` the template reads would raise, not silently N+1.
    stmt = cast("Select[Any]", select(FileRecord, *cols).options(selectinload(FileRecord.set_profile)))
    # phaze-7sdwt: `bucket is not None` is the WHOLE gate -- `stage` alone ("any status") stays a
    # deliberate no-op, matching the caller contract stated above.
    if bucket is not None:
        if bucket == ORPHANED_BUCKET:
            if stage is None:
                orphan_stages: tuple[Stage, ...] = _ORPHAN_ELIGIBLE_STAGES
            elif stage in _ORPHAN_ELIGIBLE_STAGES:
                orphan_stages = (stage,)
            else:
                orphan_stages = ()
            stmt = stmt.where(or_(*(_orphan_row_clause(s) for s in orphan_stages)) if orphan_stages else false())
        elif stage is not None:
            stmt = stmt.where(stage_status_case(stage) == bucket)
        else:
            stmt = stmt.where(or_(*(stage_status_case(s) == bucket for s in _FILES_PAGE_STAGES)))
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

    ``stage``+``bucket`` are accepted NOW (plumbed straight through to :func:`_files_page_stmt`) so the
    status filter bar remains templates-only. ``bucket`` alone filters -- "any stage = <status>",
    implemented as "some stage has that status" (phaze-7sdwt); ``stage`` alone ("any status") stays a
    no-op, since there is no status to compare against. ``bucket is not None`` is therefore the caller's
    signal that a filter actually applied -- see :func:`_files_page_stmt`.

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
            page_rows, has_next = split_sentinel(result, page_size)
            lookups = await _tracklist_lookups(session, [row[0].id for row in page_rows])
    except Exception:
        logger.warning("files_page_degraded", page=page, page_size=page_size, exc_info=True)
        return FilesPage(rows=[], page=page, page_size=page_size, has_next=False)
    rows = [
        FilesPageRow(
            file=row[0],
            buckets={stage_member.value: row[idx + 1] for idx, stage_member in enumerate(_FILES_PAGE_STAGES)},
            tracklist_lookup=lookups.get(row[0].id),
        )
        for row in page_rows
    ]
    return FilesPage(rows=rows, page=page, page_size=page_size, has_next=has_next)


async def _tracklist_lookups(session: AsyncSession, file_ids: list[uuid.UUID]) -> dict[uuid.UUID, TracklistFileLookup]:
    """Load the page rows' ``tracklist_file_lookups`` in ONE primary-key ``IN`` read (phaze-o71bf).

    Bounded by the page size, never per row: the correlated CASE above already derived each row's
    bucket, and this only fetches the detail the pill prints (the outcome word and retry date).
    """
    if not file_ids:
        return {}
    result = await session.execute(select(TracklistFileLookup).where(TracklistFileLookup.file_id.in_(file_ids)))
    return {lookup.file_id: lookup for lookup in result.scalars()}


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
