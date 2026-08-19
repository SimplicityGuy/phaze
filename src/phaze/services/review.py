"""Phase 60 (REVIEW-01/REVIEW-02): degrade-safe read helpers for the Review diff workspaces.

The Rename/Path and Move-files workspaces (Plan 60-02) render pending ``RenameProposal`` rows, and the
Tag-write workspace (Plan 60-03) renders the computed tag comparison, all through the ONE shared
``pipeline/partials/_diff_row.html`` partial (D-06). These helpers are their single read seam: each
wraps its query in a ``session.begin_nested()`` SAVEPOINT and maps every ORM row to a plain dict, so
the templates never touch an ORM object and the hot render/poll path can NEVER 500 (mirrors
:func:`phaze.services.pipeline.get_analyze_working_set`). No enqueue, no commit, no schema change.

* :func:`get_pending_proposal_rows` -- pending ``RenameProposal`` rows (Rename/Move, Plan 60-02),
  bundled with the corpus-wide pending total and the >=90%-confidence match count (phaze-rw14) --
  both real ``COUNT``s, never the length of the 200-row render cap.
* :func:`get_proposal_workspace_page` -- the FILTERED, SEARCHED, PAGINATED sibling of the above,
  plus the filter-tab counts, for the Propose workspace (phaze-a6hm.2 / .9). Same row dict shape,
  so both feed ``_file_table.html`` interchangeably.
* :func:`get_tagwrite_review_page`  -- applied files (``applied_clause()``, READ-05/D-01) with a
  pending, >=1-change tag comparison (Tag-write, Plan 60-03; Pitfall 3 -- only applied files without
  a COMPLETED ``TagWriteLog``). Each row also carries ``has_prior_write`` (phaze-o5rf) so the
  workspace only surfaces UNDO where it can actually revert something.
* :func:`get_dedupe_groups`         -- scored duplicate groups + keeper flag (Dedupe, Plan 60-04;
  keeper == ``score_group``'s ``canonical_id``; review mints an opaque plan before confirmation).
* :func:`get_cue_review_cards`      -- eligible + gated cue cards with an IN-MEMORY ``.cue`` preview
  (Cue, Plan 60-04; ``generate_cue_content`` only -- NO ``write_cue_file``, the render never mutates disk).

phaze-b4u3p: this module's DECOMPOSITION SEAM. Every public function above stays defined here with
its signature and degrade-safe contract unchanged (every ``routers/*`` caller keeps importing the
same names) -- what moved out is the query/computation surface each one leaned on:

* The tag-comparison predicate + row-building helpers this module used to reach into
  ``routers/tags.py``'s private (underscore-prefixed) surface for now live in
  ``services/tag_comparison.py``, and ``routers/tags.py`` imports them right back -- see that
  module's docstring for the full layering rationale. This resolves the SERVICE ->
  ROUTER-private-helper inversion repowise flagged: both modules now depend on one shared service
  module instead of one reaching into the other.
* Likewise the cue-eligibility query surface previously reached into ``routers/cue.py``'s private
  helpers now lives in ``services/cue_review.py``, alongside a NEW batched
  ``build_cue_tracks_for_versions`` that fixes the ``get_cue_review_cards`` cross-function N+1 (see
  that function's docstring) and a shared ``_approved_applied_tracklist_base`` that removes the
  14-line clone this module's old ``_gated_tracklist_stmt`` shared with ``routers/cue.py``'s
  eligible-set predicate.
* Within each brain method (``get_changes_review_page``, ``get_tagwrite_review_page``,
  ``get_cue_review_cards``), the per-row/per-batch dict-building logic is factored into private
  helpers defined IN THIS MODULE (not relocated) so the public function's own body stays a short,
  low-CCN orchestration of "fetch, then build rows" -- and so the existing
  ``patch("phaze.services.review.generate_cue_content", ...)``-style tests keep working: that name
  is resolved out of THIS module's globals at call time, wherever the calling function is defined.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import case, func, select, tuple_
from sqlalchemy.orm import selectinload
import structlog

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist
from phaze.services.collision import get_review_collision_ids
from phaze.services.cue_generator import generate_cue_content
from phaze.services.cue_review import build_cue_tracks_for_versions, eligible_tracklist_stmt, gated_tracklist_stmt, get_eligible_tracklist_query
from phaze.services.dedup import find_duplicate_groups_with_metadata, score_group
from phaze.services.proposal_queries import (
    Pagination,
    ProposalStats,
    count_pending_above_confidence,
    get_proposal_stats,
    get_proposals_page,
    proposal_review_digest,
)
from phaze.services.stage_status import applied_clause
from phaze.services.tag_comparison import (
    _build_comparison,
    _count_changes,
    _encode_tag_review_token,
    _get_accepted_discogs_links_for_files,
    _get_tracklists_for_files,
    _summarize_tags,
    _tag_review_payload,
    _terminal_tagwrite_subq,
)
from phaze.services.tag_proposal import compute_proposed_tags


if TYPE_CHECKING:
    import uuid

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.discogs_link import DiscogsLink
    from phaze.routers.column_sort import SortState
    from phaze.services.cue_generator import CueTrackData


logger = structlog.get_logger(__name__)

# D-03 / T-85-01: a fixed cap on the two genuinely-unbounded operator list builders below. Neither
# ``get_tagwrite_review_rows`` nor ``get_cue_review_cards`` takes an operator-supplied ``page_size``
# (they are degrade-safe render helpers), so a fixed ``.limit(...)`` is a stronger DoS control than a
# ``Query(le=100)`` bound: the now-populating applied() backlog (READ-05) can never blow up the render
# at 200K scale. Chosen low-thousands, consistent with the in-tree page bounds and ``_MAX_BULK_TAG_WRITE``.
_MAX_REVIEW_ROWS = 2000

# WR-01: the keyset scan page size for ``get_tagwrite_review_rows``. The qualifying-change filter is
# Python (``compute_proposed_tags`` over metadata+tracklist+discogs), so it cannot be pushed into SQL
# and a plain ``.limit(_MAX_REVIEW_ROWS)`` on raw candidates would cap NON-qualifying rows -- a wall of
# zero-change applied files that sort first would silently truncate the qualifying files behind them.
# Instead we keyset-page the candidate set (bounded to this many rows per DB round-trip, so the render
# never materializes the 200K applied backlog -- the D-03 memory bound is preserved) and accumulate
# only QUALIFYING rows until ``_MAX_REVIEW_ROWS``. The bulk-write path additionally marks zero-change
# files NO_OP so ``_terminal_tagwrite_subq`` evicts them, keeping this scan cheap in steady state.
_REVIEW_SCAN_BATCH = 500

# phaze-bto9: a hard cap on CANDIDATES SCANNED, not just on rows accumulated. Without it the
# ``while len(rows) < _MAX_REVIEW_ROWS`` guard never fires when few candidates qualify -- zero-change
# files ``continue`` without contributing to ``rows`` -- so the loop only ends when the candidate set
# is exhausted, i.e. after paging EVERY applied file. At the project's 200K design scale that is ~400
# round-trips of keyset paging (and, before this bead, another two to three per candidate on top),
# all inside one pooled connection held in a SAVEPOINT: the workspace never renders, and the only
# documented remedy (bulk write's NO_OP eviction) sits behind the page that will not load.
#
# The NO_OP mitigation the WR-01 comment names is unreachable from a cold start -- it is
# chicken-and-egg (the bulk-write POST comes from a template this scan must render first) and clears
# at most ``_MAX_BULK_TAG_WRITE`` files per submit, so a 200K wall needs 100 submits, each preceded
# by another full scan. A scanned cap is what makes the render degrade to a PARTIAL list instead.
#
# 40 batches x 500 = 20,000 candidates: comfortably above any realistic qualifying set (the render
# itself caps at 2,000 rows) while bounding the worst case to a fixed, small number of round-trips.
_MAX_REVIEW_SCAN_BATCHES = 40


class TagwriteReviewPage(NamedTuple):
    """The tag-write queue's rows plus the honesty flag the subcount needs (phaze-bto9, phaze-a2ytu).

    ``partial`` is True whenever the scan stopped with candidates possibly still unexamined --
    whether that's hitting :data:`_MAX_REVIEW_SCAN_BATCHES` with the walk incomplete, or hitting
    ``_MAX_REVIEW_ROWS`` before the candidate set was provably exhausted (phaze-a2ytu: the
    row-cap exit used to leave ``partial`` at its default False even though a full or
    mid-iterated batch could still hold unexamined candidates). The render is a bounded prefix
    of the queue, not the whole of it, and the workspace subcount says so rather than printing a
    number that silently understates the backlog.
    """

    rows: list[dict[str, Any]]
    partial: bool


class PendingProposalRows(NamedTuple):
    """Pending ``RenameProposal`` rows for the Rename/Move diff workspaces, plus the two corpus-wide
    counts their header and bulk-approve confirm need (phaze-rw14).

    The row LIST is capped at 200 for render (a render cap wearing a page's clothing -- proposal
    201 is not on a later page, it is simply absent from this list). ``total_pending`` and
    ``high_confidence_pending`` are NOT derived from ``len(rows)``: reporting the capped list length
    as either count is exactly the defect this bundle exists to close --

    * the workspace header used to report ``rename_proposals | length`` as "N awaiting approval",
      which stops counting at 200 for any backlog past that size;
    * the bulk-approve confirm dialog used to quote the SAME capped, confidence-unfiltered length
      as the number of proposals about to be approved at >=90% confidence. Because the row order is
      confidence-ASC (the lowest-confidence pending rows render first), the visible 200 can hold
      ZERO rows meeting the threshold while the server-side predicate approves thousands -- an
      operator confirming "200 match now" could be approving many times that.
    """

    rows: list[dict[str, Any]]
    total_pending: int
    high_confidence_pending: int


class ChangesReviewStats(NamedTuple):
    """Canonical operator vocabulary over the persisted proposal states."""

    all: int
    needs_review: int
    approved: int
    blocked: int
    rejected: int


class ChangesReviewPage(NamedTuple):
    """One bounded Changes Review proposal page and its corpus-wide status counts."""

    rows: list[dict[str, Any]]
    pagination: Pagination
    stats: ChangesReviewStats


_CHANGES_STATUS_MAP: dict[str, tuple[ProposalStatus, ...]] = {
    "needs_review": (ProposalStatus.PENDING,),
    "approved": (ProposalStatus.APPROVED, ProposalStatus.EXECUTED),
    "blocked": (ProposalStatus.FAILED,),
    "rejected": (ProposalStatus.REJECTED,),
}


async def _changes_review_stats(session: AsyncSession) -> ChangesReviewStats:
    """Run the ONE aggregate query behind both the filter-tab counts and the filtered-page total."""
    count_stmt = select(
        func.count().label("all"),
        func.count(case((RenameProposal.status == ProposalStatus.PENDING.value, 1))).label("needs_review"),
        func.count(case((RenameProposal.status.in_((ProposalStatus.APPROVED.value, ProposalStatus.EXECUTED.value)), 1))).label("approved"),
        func.count(case((RenameProposal.status == ProposalStatus.FAILED.value, 1))).label("blocked"),
        func.count(case((RenameProposal.status == ProposalStatus.REJECTED.value, 1))).label("rejected"),
    ).select_from(RenameProposal)
    aggregate = (await session.execute(count_stmt)).one()
    return ChangesReviewStats(
        all=aggregate.all,
        needs_review=aggregate.needs_review,
        approved=aggregate.approved,
        blocked=aggregate.blocked,
        rejected=aggregate.rejected,
    )


def _changes_review_warnings(proposal: RenameProposal) -> list[str]:
    """Per-proposal operator-facing caveats -- low/absent confidence, no destination change, a stored reason."""
    warnings: list[str] = []
    if proposal.confidence is None:
        warnings.append("Confidence unavailable; individual review required.")
    elif proposal.confidence < 0.9:
        warnings.append("Below the 90% bulk-approval threshold; individual review required.")
    if proposal.proposed_path is None:
        warnings.append("No destination change; the file will be renamed in its current directory.")
    if proposal.reason:
        warnings.append(proposal.reason)
    return warnings


def _build_changes_review_row(proposal: RenameProposal, collision_ids: set[str]) -> dict[str, Any]:
    """Map one ``RenameProposal`` (+ its eagerly-loaded ``file``) to the ``_diff_row.html`` row dict."""
    raw_status = ProposalStatus(proposal.status)
    review_status = next(
        (label for label, values in _CHANGES_STATUS_MAP.items() if raw_status in values),
        "blocked",
    )
    conflicts = ["Destination collides with another pending or approved operation; approval is blocked."] if str(proposal.id) in collision_ids else []
    return {
        "id": proposal.id,
        "file_id": proposal.file_id,
        "filename": proposal.file.original_filename,
        "original_path": proposal.file.current_path,
        "proposed_filename": proposal.proposed_filename,
        "proposed_path": proposal.proposed_path,
        "confidence": proposal.confidence,
        "status": review_status,
        "raw_status": proposal.status,
        "warnings": _changes_review_warnings(proposal),
        "conflicts": conflicts,
        "bulk_eligible": (raw_status == ProposalStatus.PENDING and proposal.confidence is not None and proposal.confidence >= 0.9 and not conflicts),
        "updated_at": proposal.updated_at,
        "review_token": f"{proposal.id}|{proposal.updated_at.isoformat()}|{proposal_review_digest(proposal)}",
    }


async def get_changes_review_page(
    session: AsyncSession,
    *,
    status: str,
    page: int,
    page_size: int,
) -> ChangesReviewPage:
    """Return atomic filename/destination decisions under the canonical review vocabulary.

    ``RenameProposal.status`` remains untouched: the mapping is presentation-only because persisted
    execution/audit state distinguishes APPROVED from EXECUTED even though both are resolved from a
    review perspective. Every count and page predicate is evaluated server-side.
    """
    active_status = status if status in {"all", *_CHANGES_STATUS_MAP} else "needs_review"
    try:
        async with session.begin_nested():
            stats = await _changes_review_stats(session)

            query = select(RenameProposal).options(selectinload(RenameProposal.file))
            if active_status != "all":
                query = query.where(RenameProposal.status.in_(tuple(one.value for one in _CHANGES_STATUS_MAP[active_status])))
            filtered_total = getattr(stats, active_status) if active_status != "all" else stats.all
            total_pages = max(1, (filtered_total + page_size - 1) // page_size)
            current_page = min(max(page, 1), total_pages)
            query = (
                query.order_by(RenameProposal.confidence.asc().nulls_first(), RenameProposal.id)
                .offset((current_page - 1) * page_size)
                .limit(page_size)
            )
            proposals = (await session.execute(query)).scalars().all()
            collision_ids = await get_review_collision_ids(session)
            rows = [_build_changes_review_row(proposal, collision_ids) for proposal in proposals]
            return ChangesReviewPage(
                rows=rows,
                pagination=Pagination(page=current_page, page_size=page_size, total=filtered_total),
                stats=stats,
            )
    except Exception:
        # Degrade-safe contract (module docstring): ANY failure inside this SAVEPOINT -- a bad
        # filter value slipping past the `active_status` guard, a transient connection error, an
        # unexpected ORM exception -- degrades this render to an empty first page with zeroed
        # stats rather than 500ing the Changes Review poll/render path. The router has no
        # exception handling of its own for this call, by design.
        logger.warning("changes_review_page_degraded", exc_info=True)
        return ChangesReviewPage(
            rows=[],
            pagination=Pagination(page=1, page_size=page_size, total=0),
            stats=ChangesReviewStats(all=0, needs_review=0, approved=0, blocked=0, rejected=0),
        )


def _proposal_row_base(proposal: RenameProposal) -> dict[str, Any]:
    """The ``id``/``filename``/paths/``confidence``/``status`` shape both proposal-row builders below
    share -- factored out because :func:`get_pending_proposal_rows` and
    :func:`get_proposal_workspace_page` used to build it via two independently-maintained dict
    literals differing only in :func:`_pending_proposal_row`'s one extra ``updated_at`` field (a
    clone repowise flagged; the two MUST agree, since both feed the same ``_file_table.html``).
    """
    return {
        "id": proposal.id,
        "filename": proposal.file.original_filename,
        "original_path": proposal.file.current_path,
        "proposed_filename": proposal.proposed_filename,
        "proposed_path": proposal.proposed_path,
        "confidence": proposal.confidence,
        "status": proposal.status,
    }


def _pending_proposal_row(proposal: RenameProposal) -> dict[str, Any]:
    """:func:`_proposal_row_base` plus the APPROVE button's optimistic-concurrency token (phaze-exivg)."""
    row = _proposal_row_base(proposal)
    row["updated_at"] = proposal.updated_at
    return row


async def get_pending_proposal_rows(session: AsyncSession, *, confidence_threshold: float = 0.9) -> PendingProposalRows:
    """Return pending ``RenameProposal`` rows for the diff workspaces, plus the real totals (degrade-safe).

    Reuses ``get_proposals_page(status="pending")`` inside a ``session.begin_nested()`` SAVEPOINT and
    maps each proposal (plus its ``selectinload``'d file) to a plain dict keyed for both diff facets:
    ``id`` · ``filename`` (``file.original_filename``) · ``original_path`` (``file.current_path``) ·
    ``proposed_filename`` · ``proposed_path`` · ``confidence`` · ``status`` · ``updated_at``
    (phaze-exivg -- the row's optimistic-concurrency token, round-tripped by the APPROVE button).
    Returns an all-empty/zero :class:`PendingProposalRows` on any DB error so the render/poll path
    degrades instead of 500ing (no router try/except needed).

    phaze-rw14: ``get_proposals_page`` already runs a real ``COUNT(*)`` for its ``Pagination.total``
    on every call -- that total used to be fetched and immediately discarded (bound to ``_pagination``
    and dropped). Returning it here is free: no new query, just no longer throwing away the one this
    function already ran. ``high_confidence_pending`` is one additional lightweight COUNT
    (:func:`count_pending_above_confidence`) over the >= threshold PENDING set, so a confirm dialog
    can name what a bulk action actually covers instead of the rendered page's row count. It used
    to be described as mirroring ``approve_pending_above_confidence``; phaze-7tiqp retired that
    function along with the routeless bulk-approve-high-confidence chain.
    """
    try:
        async with session.begin_nested():
            proposals, pagination = await get_proposals_page(session, status="pending", page_size=200)
            high_confidence_pending = await count_pending_above_confidence(session, threshold=confidence_threshold)
            rows = [_pending_proposal_row(proposal) for proposal in proposals]
            return PendingProposalRows(rows=rows, total_pending=pagination.total, high_confidence_pending=high_confidence_pending)
    except Exception:
        # Degrade-safe contract (module docstring): a corpus-wide COUNT (via get_proposals_page /
        # count_pending_above_confidence) failing here degrades the Rename/Move diff workspace to
        # an all-empty/zero bundle rather than 500ing the render/poll path.
        logger.warning("pending_proposal_rows_degraded", exc_info=True)
        return PendingProposalRows(rows=[], total_pending=0, high_confidence_pending=0)


class ProposalWorkspacePage(NamedTuple):
    """One filtered, searched, paginated page of the Propose workspace, plus the tab counts.

    ``stats`` is bundled with the rows rather than fetched separately by the router because the
    filter tabs and the pager are two halves of ONE answer to "what am I looking at": the tabs
    report the corpus-wide counts, the pager reports the filtered total. Fetching them through one
    degrade-safe seam means they can never disagree about whether the read succeeded -- a partial
    failure that left real tab counts above an empty table would read as "23 pending" over "no
    proposals", which is a lie the operator has no way to diagnose.
    """

    rows: list[dict[str, Any]]
    pagination: Pagination
    stats: ProposalStats


async def get_proposal_workspace_page(
    session: AsyncSession,
    *,
    status: str,
    search: str,
    page: int,
    page_size: int,
    sort: SortState | None = None,
) -> ProposalWorkspacePage:
    """Return one page of proposals for the Propose workspace, with tab counts (degrade-safe).

    The paginated sibling of :func:`get_pending_proposal_rows`, and the read behind
    ``/s/propose``'s filter tabs, search box and pager (phaze-a6hm.2 / .9). It emits the SAME row
    dict shape that helper does -- ``id`` · ``filename`` · ``original_path`` · ``proposed_filename``
    · ``proposed_path`` · ``confidence`` · ``status`` -- so ``_file_table.html`` and the workspaces built on it
    are unaffected by which of the two produced the rows.

    Three differences from ``get_pending_proposal_rows``, all of them the point of this function:

    * the status filter is the OPERATOR's, not hardcoded ``"pending"``;
    * ``search`` is threaded into the query rather than dropped;
    * the page is a real page. ``get_pending_proposal_rows`` passes ``page_size=200``, which is a
      cap wearing a page's clothing: proposal 201 is not on a later page, it is simply absent, and
      nothing in the UI says so. Here ``page_size`` is bounded by ``ListViewState``
      (``PAGE_SIZE_CHOICES``) and the returned :class:`Pagination` carries the real total, so every
      row is reachable and the count the pager prints is the count the filter actually matched.

    ``sort`` arrives ALREADY RESOLVED (phaze-a6hm.10). It is a ``SortState`` produced by the router
    from ``proposal_sort.PROPOSE_SORT``, not the raw wire strings this function used to take, so
    neither this function nor ``get_proposals_page`` holds a whitelist -- there is one, in one
    place, and an unrecognised ``sort`` was already degraded to the default before it got here.
    Passing the strings through to be validated downstream, as this used to, is what let a second
    validation ladder grow in the query layer.

    The whole read runs in one ``session.begin_nested()`` SAVEPOINT and degrades to an empty first
    page with zeroed stats on ANY DB error, so the render path can never 500 (no router
    try/except needed) -- identical in contract to its siblings above.

    Args:
        session: Active async session.
        status: Status filter; ``"all"`` for unfiltered.
        search: Free-text filename search; empty string for none.
        page: 1-based page number.
        page_size: Rows per page.
        sort: A resolved ``SortState``, or ``None`` for the default confidence ordering.

    Returns:
        A :class:`ProposalWorkspacePage`. Never raises.
    """
    try:
        async with session.begin_nested():
            proposals, pagination = await get_proposals_page(
                session,
                status=status,
                search=search or None,
                page=page,
                page_size=page_size,
                sort=sort,
            )
            stats = await get_proposal_stats(session)
            rows = [_proposal_row_base(proposal) for proposal in proposals]
            return ProposalWorkspacePage(rows=rows, pagination=pagination, stats=stats)
    except Exception:
        # Degrade-safe contract (module docstring): a filter/search/sort combination reaching an
        # unexpected DB error degrades the Propose workspace to an empty first page with zeroed
        # tab stats, rather than 500ing -- the SAME contract as every sibling reader in this module.
        logger.warning("proposal_workspace_page_degraded", exc_info=True)
        return ProposalWorkspacePage(
            rows=[],
            pagination=Pagination(page=1, page_size=page_size, total=0),
            stats=ProposalStats(total=0, pending=0, approved=0, executed=0, rejected=0, failed=0, avg_confidence=None),
        )


async def _fetch_tagwrite_batch(
    session: AsyncSession,
    terminal_subq: Select[tuple[uuid.UUID]],
    last_key: tuple[str, Any] | None,
) -> list[FileRecord]:
    """One WR-01 keyset-paged scan batch: up to ``_REVIEW_SCAN_BATCH`` applied, non-terminal candidates.

    Ordered + ranged on the ``(original_filename, id)`` btree (migration 048); ``last_key`` is the
    previous batch's last row, or ``None`` for the first page.
    """
    stmt = (
        select(FileRecord)
        .options(selectinload(FileRecord.file_metadata))
        .where(applied_clause(), FileRecord.id.not_in(terminal_subq))
        .order_by(FileRecord.original_filename, FileRecord.id)
        .limit(_REVIEW_SCAN_BATCH)
    )
    if last_key is not None:
        stmt = stmt.where(tuple_(FileRecord.original_filename, FileRecord.id) > last_key)
    return list((await session.execute(stmt)).scalars().all())


async def _fetch_tagwrite_batch_logs(session: AsyncSession, batch_ids: list[uuid.UUID]) -> dict[uuid.UUID, TagWriteLog]:
    """phaze-o5rf: batch-fetch which of THIS scan batch's files already carry a ``TagWriteLog``.

    One round-trip per scan batch rather than per row -- can only resolve to a QUEUED/DISCREPANCY/
    FAILED entry, since ``terminal_subq`` already excluded every COMPLETED/NO_OP file upstream.
    """
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


def _build_tagwrite_row(
    fr: FileRecord,
    tracklist: Tracklist | None,
    discogs_link: DiscogsLink | None,
    latest_logs: dict[uuid.UUID, TagWriteLog],
) -> dict[str, Any] | None:
    """Build ONE tag-write queue row, or ``None`` if the file's server-computed comparison has zero changes.

    Mirrors ``tags.write_file_tags``: ``compute_proposed_tags`` over the file's metadata +
    tracklist + accepted Discogs link, then ``_build_comparison`` / ``_count_changes``.
    ``has_prior_write`` (phaze-o5rf) is True iff ``latest_logs`` already carries an entry for this
    file -- since the caller's ``terminal_subq`` already evicted every COMPLETED/NO_OP file, that
    entry can only be QUEUED/DISCREPANCY/FAILED.
    """
    proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename, discogs_link=discogs_link)
    comparison = _build_comparison(fr.file_metadata, proposed)
    changed_count = _count_changes(comparison)
    if changed_count < 1:
        return None
    latest = latest_logs.get(fr.id)
    has_blanking = any(c["current"] is not None and c["proposed"] is None for c in comparison)
    return {
        "file_id": fr.id,
        "filename": fr.original_filename,
        "before_summary": _summarize_tags(comparison, "current"),
        "after_summary": _summarize_tags(comparison, "proposed"),
        "review_token": _encode_tag_review_token(_tag_review_payload(fr, tracklist, discogs_link, proposed)),
        "changed_count": changed_count,
        "has_blanking": has_blanking,
        "has_prior_write": latest is not None,
        "latest_status": latest.status if latest is not None else None,
        "bulk_eligible": not has_blanking and (latest is None or (latest.source == "undo" and latest.status == TagWriteStatus.COMPLETED.value)),
        "status": ("blocked" if latest is not None and latest.status in {"failed", "discrepancy", "verify_failed"} else "needs_review"),
        "discrepancies": latest.discrepancies if latest is not None else None,
        "error_message": latest.error_message if latest is not None else None,
    }


def _rows_from_tagwrite_batch(
    batch: list[FileRecord],
    tracklists: dict[uuid.UUID, Tracklist],
    discogs_links: dict[uuid.UUID, DiscogsLink],
    latest_logs: dict[uuid.UUID, TagWriteLog],
    remaining_capacity: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Build up to ``remaining_capacity`` qualifying rows from ONE scan batch.

    Factored out of :func:`get_tagwrite_review_page`'s while-loop body so that function's own
    nesting stays ``try -> async with -> while`` (3 deep) instead of running the per-row build and
    its mid-batch cap check a level further in (the ``nested_complexity`` finding this bead is
    closing). Returns ``(rows, row_cap_hit_mid_batch)`` -- the second element is phaze-a2ytu's
    mid-batch cap signal: True iff the cap landed with unexamined candidates still in THIS batch
    (an unexamined tail means the render is a bounded prefix, not the whole queue).
    """
    rows: list[dict[str, Any]] = []
    for i, fr in enumerate(batch):
        row = _build_tagwrite_row(fr, tracklists.get(fr.id), discogs_links.get(fr.id), latest_logs)
        if row is None:
            continue
        rows.append(row)
        if len(rows) >= remaining_capacity:
            return rows, i < len(batch) - 1
    return rows, False


async def get_tagwrite_review_page(session: AsyncSession) -> TagwriteReviewPage:
    """Return the pending tag-write review rows for the Tag-write workspace (degrade-safe, bounded).

    Surfaces ONLY applied files (READ-05/D-01 -- an ``executed`` ``RenameProposal`` exists, via
    ``applied_clause()``; the file's ``state`` column is NEVER read) that have NO terminal
    ``TagWriteLog`` (Pitfall 3 -- a file still awaiting a move never appears, so an empty queue is
    CORRECT, not a bug), bounded by ``_MAX_REVIEW_ROWS`` (D-03), and whose server-computed tag
    comparison has ``>= 1`` change (there is something to write). For each it mirrors
    ``tags.write_file_tags``: ``compute_proposed_tags`` over the file's metadata + tracklist +
    accepted Discogs link, then ``_build_comparison`` / ``_count_changes``. The whole read runs
    inside a ``session.begin_nested()`` SAVEPOINT and returns an empty page on any error so the
    render/poll path degrades instead of 500ing (no router try/except needed). Per row: ``file_id``
    · ``filename`` · ``before_summary`` (current tags joined) · ``after_summary`` (proposed tags
    joined) · ``changed_count`` · ``has_blanking`` (any field whose current value would be erased) ·
    ``has_prior_write`` (phaze-o5rf: True iff the file already carries a ``TagWriteLog`` -- since
    ``_terminal_tagwrite_subq`` already evicted every COMPLETED/NO_OP file from the candidate window,
    a row reaching here with a log can only hold a QUEUED/DISCREPANCY/FAILED entry). No enqueue, no
    commit, no write.

    **Cost shape (phaze-bto9).** Two bounds, both necessary and neither sufficient alone:

    * Per SCAN BATCH the tracklist and accepted-Discogs lookups are TWO queries keyed by
      ``file_id IN (...)``, mirroring the ``logged_ids`` batch that was already here. They used to
      be two-to-three queries PER CANDIDATE, so a 200K applied backlog of already-correctly-tagged
      files issued ~500K round-trips to render a page that qualifies almost nothing.
    * The scan stops after :data:`_MAX_REVIEW_SCAN_BATCHES` batches and reports ``partial=True``.
      The accumulate-only-qualifying-rows design (WR-01) means the row cap alone can never terminate
      a scan over a corpus where few candidates qualify -- the walk is linear and unbounded in the
      applied backlog, which is a TIME bound the docstring's "the D-03 memory bound is preserved"
      claim never covered (it is true for memory, and was false for time).

    Both rest on the ``(original_filename, id)`` btree added in migration 048: the keyset paging
    orders and ranges on exactly that tuple, and without it each batch re-scanned and re-sorted the
    whole ``files`` table (the only ``original_filename`` index is a GIN trgm one, which cannot serve
    an ordered range), making the paging itself the dominant cost.
    """
    try:
        async with session.begin_nested():
            terminal_subq = _terminal_tagwrite_subq()
            rows: list[dict[str, Any]] = []
            partial = False
            # WR-01: accumulate QUALIFYING rows up to the cap by keyset-paging the candidate set on
            # ``(original_filename, id)`` (id breaks ties on the non-unique filename), instead of
            # ``.limit(_MAX_REVIEW_ROWS)``-ing raw candidates and dropping the non-qualifying majority.
            # This surfaces a qualifying file even when it sorts behind a wall of zero-change files,
            # while bounding memory to ``_REVIEW_SCAN_BATCH`` rows per round-trip (D-03).
            last_key: tuple[str, Any] | None = None
            batches = 0
            while len(rows) < _MAX_REVIEW_ROWS:
                if batches >= _MAX_REVIEW_SCAN_BATCHES:
                    # phaze-bto9: candidates remain, but the walk is capped. Report a PARTIAL list --
                    # a usable prefix of the queue -- rather than holding a pooled connection inside
                    # a SAVEPOINT for the rest of the applied backlog and rendering nothing at all.
                    partial = True
                    break
                batch = await _fetch_tagwrite_batch(session, terminal_subq, last_key)
                if not batch:
                    break
                batches += 1
                last_key = (batch[-1].original_filename, batch[-1].id)
                batch_ids = [fr.id for fr in batch]
                latest_logs = await _fetch_tagwrite_batch_logs(session, batch_ids)
                # phaze-bto9: the same batching, extended to the two lookups that were still per-row.
                tracklists = await _get_tracklists_for_files(session, batch_ids)
                discogs_links = await _get_accepted_discogs_links_for_files(session, tracklists)
                # phaze-a2ytu: hitting the row cap mid-scan means candidates may remain -- unless
                # this batch was BOTH fully consumed (no unexamined tail) AND short (< the batch
                # size, so the candidate set itself just ran out here). See
                # :func:`_rows_from_tagwrite_batch` for the mid-batch signal itself.
                new_rows, row_cap_hit_mid_batch = _rows_from_tagwrite_batch(
                    batch, tracklists, discogs_links, latest_logs, _MAX_REVIEW_ROWS - len(rows)
                )
                rows.extend(new_rows)
                if len(rows) >= _MAX_REVIEW_ROWS:
                    if row_cap_hit_mid_batch or len(batch) == _REVIEW_SCAN_BATCH:
                        partial = True
                    break
                if len(batch) < _REVIEW_SCAN_BATCH:
                    break  # candidate set exhausted
            return TagwriteReviewPage(rows=rows, partial=partial)
    except Exception:
        # Degrade-safe contract (module docstring): a failure ANYWHERE in the scan -- the keyset
        # page, the batched log/tracklist/Discogs lookups, or the per-row comparison build --
        # degrades the Tag-write workspace to an empty, non-partial page rather than 500ing the
        # render/poll path. Deliberately broad: this is the router's ONLY seam onto this data, by
        # design (see the module docstring), so there is no narrower boundary to catch at.
        logger.warning("tagwrite_review_rows_degraded", exc_info=True)
        return TagwriteReviewPage(rows=[], partial=False)


async def get_tagwrite_review_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Rows-only view of :func:`get_tagwrite_review_page` (unchanged legacy shape)."""
    return (await get_tagwrite_review_page(session)).rows


def _format_size(num_bytes: int | None) -> str:
    """Render a byte count as a short human-readable size string (``"22.4 MB"``); ``"unknown size"`` if absent."""
    if not num_bytes:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _format_quality(file_dict: dict[str, Any]) -> str:
    """Render a duplicate file's quality summary (``"320 kbps · 22.4 MB"``), omitting an absent bitrate.

    ``bitrate`` is stored in BITS per second (phaze-iw2k -- matching what mutagen actually
    reports); divide by 1000 here to render kbps.
    """
    size = _format_size(file_dict.get("file_size"))
    bitrate = file_dict.get("bitrate")
    if bitrate:
        return f"{bitrate // 1000} kbps · {size}"
    return size


def build_dupe_group_card(group: dict[str, Any]) -> dict[str, Any]:
    """Map a SCORED duplicate group dict into the ``_dupe_group.html`` card shape.

    Assumes ``score_group`` has already run on ``group`` (sets ``group["canonical_id"]`` and sorts
    ``group["files"]`` keeper-first). Returns ``sha256_hash`` (the group key the keeper radio
    resolves against -- ``POST /duplicates/{sha256_hash}/resolve`` with Form ``canonical_id``), a
    short ``group_name`` label, ``count``, ``truncated``, and ``files`` (each ``id`` · ``name`` ·
    ``quality`` · ``keeper`` where ``keeper == (id == canonical_id)``). The card posts canonical_id
    to the review endpoint; the destructive endpoint accepts only the resulting opaque plan id.

    Shared by :func:`get_dedupe_groups` (the whole-list Dedupe workspace read) and the
    ``POST /duplicates/{hash}/undo`` router (phaze-be1j): undo must swap a restored group back
    into the live workspace using this SAME shell shape -- rendering the legacy
    ``group_card.html`` accordion row there left the toast's Undo unable to hand the restored
    group a working keeper-select card.

    phaze-z4p5q: ``group["truncated"]`` (set by ``services/dedup.py``'s per-member cap on the
    underlying read) rides straight through to the card so ``_dupe_group.html`` can flag a group
    whose real membership is larger than what's actually shown here. Missing on ``group`` (a caller
    that built the dict some other way) degrades to ``False`` rather than raising.
    """
    canonical_id = group["canonical_id"]
    files = group["files"]
    return {
        "sha256_hash": group["sha256_hash"],
        "group_name": Path(files[0]["original_path"]).name if files else group["sha256_hash"][:12],
        "count": len(files),
        "truncated": group.get("truncated", False),
        "rationale": group.get("rationale", "highest-quality ranking"),
        "files": [
            {
                "id": f["id"],
                "name": Path(f["original_path"]).name,
                "path": f["original_path"],
                "quality": _format_quality(f),
                "duration": f.get("duration"),
                "tag_label": f.get("tag_label", "None"),
                "keeper": f["id"] == canonical_id,
            }
            for f in files
        ],
    }


async def get_dedupe_groups(session: AsyncSession) -> list[dict[str, Any]]:
    """Return scored duplicate groups as plain dicts for the Dedupe keeper-select workspace (degrade-safe).

    Reuses ``find_duplicate_groups_with_metadata`` + ``score_group`` (which sets ``group["canonical_id"]``
    to the highest-quality copy) inside a ``session.begin_nested()`` SAVEPOINT, and maps each group via
    :func:`build_dupe_group_card` to the plain dict the ``_dupe_group.html`` card consumes. Returns ``[]``
    on any DB error so the render/poll path degrades instead of 500ing (no router try/except needed). No
    enqueue, no commit, no write.
    """
    try:
        async with session.begin_nested():
            groups = await find_duplicate_groups_with_metadata(session)
            cards: list[dict[str, Any]] = []
            for group in groups:
                score_group(group)  # sets group["canonical_id"] + sorts files keeper-first
                cards.append(build_dupe_group_card(group))
            return cards
    except Exception:
        # Degrade-safe contract (module docstring): a failure in either
        # find_duplicate_groups_with_metadata or score_group degrades the Dedupe workspace to an
        # empty list rather than 500ing the render/poll path.
        logger.warning("dedupe_groups_degraded", exc_info=True)
        return []


# phaze-b4u3p: the gated-tracklist predicate (formerly ``_gated_tracklist_stmt``) moved to
# ``services/cue_review.py`` as ``gated_tracklist_stmt`` (imported above), built on the SAME shared
# join+filter core ``eligible_tracklist_stmt`` uses -- see that module's docstring. It previously
# duplicated ``routers/cue.py``'s eligible-set predicate almost verbatim (differing only in the one
# ``latest_version_id`` clause), which was the 14-line clone repowise flagged between the two files.


async def count_cue_review_candidates(session: AsyncSession) -> int:
    """Count cue review candidates (eligible + gated) corpus-wide -- two aggregates, NO cue text built.

    phaze-tzy6s.17: this exists because ``len(await get_cue_review_cards(session))`` is not a corpus
    count. That reader caps each half at :data:`_MAX_REVIEW_ROWS` (WR-04), so it saturates at
    ``2 * _MAX_REVIEW_ROWS`` and reports the ceiling as though it were the total.

    It is also enormously cheaper, which is the other half of the same defect. Producing that
    ``len()`` ran ``_build_cue_tracks`` + ``generate_cue_content`` for up to ``_MAX_REVIEW_ROWS``
    sets -- full in-memory ``.cue`` generation for thousands of concert sets -- and then discarded
    every string. Eligibility is entirely SQL-expressible, so none of that work is needed to answer
    "how many are there".

    Degrade-safe by the same contract as its sibling readers: on any DB error it logs and returns 0,
    which drops the exclusion row rather than rendering a number it did not measure.
    """
    try:
        async with session.begin_nested():
            eligible = await session.execute(select(func.count()).select_from(eligible_tracklist_stmt().subquery()))
            gated = await session.execute(select(func.count()).select_from(gated_tracklist_stmt().subquery()))
            return int(eligible.scalar_one()) + int(gated.scalar_one())
    except Exception:
        # Degrade-safe contract (module docstring): a failure in either aggregate degrades the
        # exclusion-row count to 0 rather than 500ing -- dropping the row is safer than rendering a
        # number this read did not actually measure.
        logger.warning("cue_review_count_degraded", exc_info=True)
        return 0


def _build_eligible_cue_card(
    tracklist: Tracklist,
    file_record: FileRecord,
    cue_tracks_by_version: dict[uuid.UUID, list[CueTrackData]],
) -> dict[str, Any]:
    """Build one ELIGIBLE (or per-card-degraded) preview card from an ALREADY-FETCHED cue-track list.

    phaze-b4u3p: ``cue_tracks_by_version`` is built ONCE, upfront, for the whole eligible half by
    :func:`phaze.services.cue_review.build_cue_tracks_for_versions` -- this is the fix for the
    cross-function N+1 this function used to have (one ``_build_cue_tracks`` DB round-trip PER
    card; see that function's docstring for the measured shape). Only ``generate_cue_content``
    (the in-memory ``.cue`` text render, T-60-CUE -- no disk write) can still raise per card here,
    so it alone is isolated with its own try/except (phaze-hcsb): ONE bad card degrades to the
    gated shape instead of blanking the whole workspace, exactly as before. A failure in the
    UPFRONT batched fetch itself (now a single query pair for the whole page, not one per card) is
    caught by the OUTER ``session.begin_nested()`` handler instead, same as every other batched
    read in this module -- consistent with, not a regression from, the rest of the degrade
    contract.
    """
    try:
        cue_text: str | None = None
        if tracklist.latest_version_id:
            cue_tracks = cue_tracks_by_version.get(tracklist.latest_version_id, [])
            audio_name = Path(file_record.current_path).name
            cue_text = generate_cue_content(audio_name, file_record.file_type, cue_tracks)
    except Exception:
        logger.warning("cue_review_card_build_failed", tracklist_id=str(tracklist.id), exc_info=True)
        # Degrade this ONE card to the gated shape (no approve control, no stale/partial preview)
        # instead of dropping the whole render.
        return {
            "tracklist_id": tracklist.id,
            "file_id": file_record.id,
            "set_name": Path(file_record.current_path).stem,
            "eligible": False,
            "build_error": True,
            "cue_text": None,
            "version_id": None,
        }
    return {
        "tracklist_id": tracklist.id,
        "file_id": file_record.id,
        "set_name": Path(file_record.current_path).stem,
        "eligible": True,
        "build_error": False,
        "cue_text": cue_text,
        # phaze-ce65s: the version THIS preview's cue_text was actually built from -- carried back
        # on APPROVE (hx-vals) so the route can refuse a write if `latest_version_id` moved before
        # the click.
        "version_id": tracklist.latest_version_id,
    }


async def _gated_cue_cards(session: AsyncSession) -> list[dict[str, Any]]:
    """The GATED half of the Cue workspace: approved + applied() file, no timestamped track on the LATEST version.

    Mirrors the eligibility gate's ``latest_version_id`` scoping (phaze-dboy) via the shared
    :func:`phaze.services.cue_review.gated_tracklist_stmt`. WR-04: its own per-set cap, independent
    of the eligible half's (the intentional total ceiling is ``2 * _MAX_REVIEW_ROWS``, both halves
    SQL-bounded).
    """
    gated_stmt = gated_tracklist_stmt().order_by(Tracklist.artist, Tracklist.event).limit(_MAX_REVIEW_ROWS)
    return [
        {
            "tracklist_id": tracklist.id,
            "file_id": file_record.id,
            "set_name": Path(file_record.current_path).stem,
            "eligible": False,
            "build_error": False,
            "cue_text": None,
            "version_id": None,
        }
        for tracklist, file_record in (await session.execute(gated_stmt)).tuples().all()
    ]


async def get_cue_review_cards(session: AsyncSession) -> list[dict[str, Any]]:
    """Return eligible + gated cue cards for the Cue preview workspace (degrade-safe, NO disk write).

    Surfaces two sets, both approved tracklists on an applied file (READ-05/D-01 -- an ``executed``
    ``RenameProposal`` exists, via ``applied_clause()``; the file's ``state`` column is NEVER read).
    WR-04: ``_MAX_REVIEW_ROWS`` is a PER-SET cap, not a single render budget -- the eligible and gated
    halves are each independently bounded by it, so the returned list holds up to ``2 *
    _MAX_REVIEW_ROWS`` cards (the intentional total ceiling, both halves SQL-bounded per WR-03):

    * **eligible** -- ``>= 1`` timestamped track (``get_eligible_tracklist_query``). phaze-b4u3p: the
      ``.cue`` preview text for the WHOLE eligible half is built from ONE upfront batched fetch
      (:func:`phaze.services.cue_review.build_cue_tracks_for_versions`) -- fixing what used to be a
      cross-function N+1 (one DB round-trip pair PER eligible tracklist). It stays ENTIRELY IN
      MEMORY (T-60-CUE; the write happens only on an explicit Generate ->
      ``POST /cue/{id}/generate``, which queues the write -- there is no /approve route).
    * **gated** -- approved + applied but NO timestamped track (the "awaiting tracklist match…" ineligible
      card, rendered ``opacity-60`` with no approve control).

    The whole read runs inside a ``session.begin_nested()`` SAVEPOINT and returns ``[]`` on any error so the
    render/poll path degrades instead of 500ing (no router try/except needed). Per card:
    ``tracklist_id`` · ``set_name`` (the audio file stem, matching the generated ``.cue`` name) ·
    ``eligible`` (bool) · ``cue_text`` (the in-memory ``.cue`` string, or ``None`` for a gated card) ·
    ``version_id`` (the ``latest_version_id`` this card's preview was built from, or ``None`` for a
    gated/degraded card -- phaze-ce65s: carried back on APPROVE so the write route can detect a
    version that moved between this render and the click).
    """
    try:
        async with session.begin_nested():
            # WR-03: bound the eligible half at the SQL level so the DB never returns more than the
            # render cap; the slice below is the same defensive D-03 double-cap the original
            # loop-break carried, now a no-op in the ordinary case since the SQL LIMIT already holds.
            eligible_pairs = (await get_eligible_tracklist_query(session, limit=_MAX_REVIEW_ROWS))[:_MAX_REVIEW_ROWS]
            version_ids = [tracklist.latest_version_id for tracklist, _ in eligible_pairs if tracklist.latest_version_id]
            cue_tracks_by_version = await build_cue_tracks_for_versions(session, version_ids)
            cards = [_build_eligible_cue_card(tracklist, file_record, cue_tracks_by_version) for tracklist, file_record in eligible_pairs]

            cards.extend(await _gated_cue_cards(session))
            return cards
    except Exception:
        # Degrade-safe contract (module docstring): a failure ANYWHERE in the two upfront batched
        # fetches (eligible tracklists, their cue tracks) or the gated-set query degrades the whole
        # Cue workspace to an empty list rather than 500ing the render/poll path. A single bad
        # CARD's ``generate_cue_content`` failure never reaches here -- see
        # :func:`_build_eligible_cue_card`.
        logger.warning("cue_review_cards_degraded", exc_info=True)
        return []
