"""Phase 60 (REVIEW-01/REVIEW-02): degrade-safe read helpers for the Review diff workspaces.

The Rename/Path and Move-files workspaces (Plan 60-02) render pending ``RenameProposal`` rows, and the
Tag-write workspace (Plan 60-03) renders the computed tag comparison, all through the ONE shared
``pipeline/partials/_diff_row.html`` partial (D-06). These helpers are their single read seam: each
wraps its query in a ``session.begin_nested()`` SAVEPOINT and maps every ORM row to a plain dict, so
the templates never touch an ORM object and the hot render/poll path can NEVER 500 (mirrors
:func:`phaze.services.pipeline.get_analyze_working_set`). No enqueue, no commit, no schema change.

* :func:`get_pending_proposal_rows` -- pending ``RenameProposal`` rows (Rename/Move, Plan 60-02).
* :func:`get_proposal_workspace_page` -- the FILTERED, SEARCHED, PAGINATED sibling of the above,
  plus the filter-tab counts, for the Propose workspace (phaze-a6hm.2 / .9). Same row dict shape,
  so both feed ``_file_table.html`` interchangeably.
* :func:`get_tagwrite_review_rows`  -- applied files (``applied_clause()``, READ-05/D-01) with a
  pending, >=1-change tag comparison (Tag-write, Plan 60-03; Pitfall 3 -- only applied files without
  a COMPLETED ``TagWriteLog``).
* :func:`get_dedupe_groups`         -- scored duplicate groups + keeper flag (Dedupe, Plan 60-04;
  keeper == ``score_group``'s ``canonical_id``; the radio resolves via ``/duplicates/{hash}/resolve``).
* :func:`get_cue_review_cards`      -- eligible + gated cue cards with an IN-MEMORY ``.cue`` preview
  (Cue, Plan 60-04; ``generate_cue_content`` only -- NO ``write_cue_file``, the render never mutates disk).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import select, tuple_
from sqlalchemy.orm import selectinload
import structlog

from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers.cue import _build_cue_tracks, _get_eligible_tracklist_query
from phaze.routers.tags import (
    _build_comparison,
    _count_changes,
    _get_accepted_discogs_link,
    _get_tracklist_for_file,
    _summarize_tags,
    _terminal_tagwrite_subq,
)
from phaze.services.cue_generator import generate_cue_content
from phaze.services.dedup import find_duplicate_groups_with_metadata, score_group
from phaze.services.proposal_queries import Pagination, ProposalStats, get_proposal_stats, get_proposals_page
from phaze.services.stage_status import applied_clause
from phaze.services.tag_proposal import compute_proposed_tags


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.routers.column_sort import SortState


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


async def get_pending_proposal_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Return pending ``RenameProposal`` rows as plain dicts for the diff workspaces (degrade-safe).

    Reuses ``get_proposals_page(status="pending")`` inside a ``session.begin_nested()`` SAVEPOINT and
    maps each proposal (plus its ``selectinload``'d file) to a plain dict keyed for both diff facets:
    ``id`` · ``filename`` (``file.original_filename``) · ``original_path`` (``file.current_path``) ·
    ``proposed_filename`` · ``proposed_path`` · ``confidence``. Returns ``[]`` on any DB error so the
    render/poll path degrades instead of 500ing (no router try/except needed).
    """
    try:
        async with session.begin_nested():
            proposals, _pagination = await get_proposals_page(session, status="pending", page_size=200)
            return [
                {
                    "id": proposal.id,
                    "filename": proposal.file.original_filename,
                    "original_path": proposal.file.current_path,
                    "proposed_filename": proposal.proposed_filename,
                    "proposed_path": proposal.proposed_path,
                    "confidence": proposal.confidence,
                }
                for proposal in proposals
            ]
    except Exception:
        logger.warning("pending_proposal_rows_degraded", exc_info=True)
        return []


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
    · ``proposed_path`` · ``confidence`` -- so ``_file_table.html`` and the workspaces built on it
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
            rows = [
                {
                    "id": proposal.id,
                    "filename": proposal.file.original_filename,
                    "original_path": proposal.file.current_path,
                    "proposed_filename": proposal.proposed_filename,
                    "proposed_path": proposal.proposed_path,
                    "confidence": proposal.confidence,
                }
                for proposal in proposals
            ]
            return ProposalWorkspacePage(rows=rows, pagination=pagination, stats=stats)
    except Exception:
        logger.warning("proposal_workspace_page_degraded", exc_info=True)
        return ProposalWorkspacePage(
            rows=[],
            pagination=Pagination(page=1, page_size=page_size, total=0),
            stats=ProposalStats(total=0, pending=0, approved=0, rejected=0, avg_confidence=None),
        )


async def get_tagwrite_review_rows(session: AsyncSession) -> list[dict[str, Any]]:
    """Return the pending tag-write review rows as plain dicts for the Tag-write workspace (degrade-safe).

    Surfaces ONLY applied files (READ-05/D-01 -- an ``executed`` ``RenameProposal`` exists, via
    ``applied_clause()``; the file's ``state`` column is NEVER read) that have NO ``COMPLETED``
    ``TagWriteLog`` (Pitfall 3 -- a file still awaiting a move never appears, so an empty queue is
    CORRECT, not a bug), bounded by ``_MAX_REVIEW_ROWS`` (D-03), and whose
    server-computed tag comparison has ``>= 1`` change (there is something to write). For each it mirrors
    ``tags.list_tags``: ``compute_proposed_tags`` over the file's metadata + tracklist + accepted Discogs
    link, then ``_build_comparison`` / ``_count_changes``. The whole read runs inside a
    ``session.begin_nested()`` SAVEPOINT and returns ``[]`` on any error so the render/poll path degrades
    instead of 500ing (no router try/except needed). Per row: ``file_id`` · ``filename`` ·
    ``before_summary`` (current tags joined) · ``after_summary`` (proposed tags joined) · ``changed_count``
    · ``has_blanking`` (any field whose current value would be erased). No enqueue, no commit, no write.
    """
    try:
        async with session.begin_nested():
            terminal_subq = _terminal_tagwrite_subq()
            rows: list[dict[str, Any]] = []
            # WR-01: accumulate QUALIFYING rows up to the cap by keyset-paging the candidate set on
            # ``(original_filename, id)`` (id breaks ties on the non-unique filename), instead of
            # ``.limit(_MAX_REVIEW_ROWS)``-ing raw candidates and dropping the non-qualifying majority.
            # This surfaces a qualifying file even when it sorts behind a wall of zero-change files,
            # while bounding memory to ``_REVIEW_SCAN_BATCH`` rows per round-trip (D-03).
            last_key: tuple[str, Any] | None = None
            while len(rows) < _MAX_REVIEW_ROWS:
                stmt = (
                    select(FileRecord)
                    .options(selectinload(FileRecord.file_metadata))
                    .where(applied_clause(), FileRecord.id.not_in(terminal_subq))
                    .order_by(FileRecord.original_filename, FileRecord.id)
                    .limit(_REVIEW_SCAN_BATCH)
                )
                if last_key is not None:
                    stmt = stmt.where(tuple_(FileRecord.original_filename, FileRecord.id) > last_key)
                batch = list((await session.execute(stmt)).scalars().all())
                if not batch:
                    break
                last_key = (batch[-1].original_filename, batch[-1].id)
                for fr in batch:
                    tracklist = await _get_tracklist_for_file(session, fr.id)
                    discogs_link = await _get_accepted_discogs_link(session, fr.id)
                    proposed = compute_proposed_tags(fr.file_metadata, tracklist, fr.original_filename, discogs_link=discogs_link)
                    comparison = _build_comparison(fr.file_metadata, proposed)
                    changed_count = _count_changes(comparison)
                    if changed_count < 1:
                        continue
                    rows.append(
                        {
                            "file_id": fr.id,
                            "filename": fr.original_filename,
                            "before_summary": _summarize_tags(comparison, "current"),
                            "after_summary": _summarize_tags(comparison, "proposed"),
                            "changed_count": changed_count,
                            "has_blanking": any(c["current"] is not None and c["proposed"] is None for c in comparison),
                        }
                    )
                    if len(rows) >= _MAX_REVIEW_ROWS:
                        break
                if len(batch) < _REVIEW_SCAN_BATCH:
                    break  # candidate set exhausted
            return rows
    except Exception:
        logger.warning("tagwrite_review_rows_degraded", exc_info=True)
        return []


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
    """Render a duplicate file's quality summary (``"320 kbps · 22.4 MB"``), omitting an absent bitrate."""
    size = _format_size(file_dict.get("file_size"))
    bitrate = file_dict.get("bitrate")
    if bitrate:
        return f"{bitrate} kbps · {size}"
    return size


def build_dupe_group_card(group: dict[str, Any]) -> dict[str, Any]:
    """Map a SCORED duplicate group dict into the ``_dupe_group.html`` card shape.

    Assumes ``score_group`` has already run on ``group`` (sets ``group["canonical_id"]`` and sorts
    ``group["files"]`` keeper-first). Returns ``sha256_hash`` (the group key the keeper radio
    resolves against -- ``POST /duplicates/{sha256_hash}/resolve`` with Form ``canonical_id``), a
    short ``group_name`` label, ``count``, and ``files`` (each ``id`` · ``name`` · ``quality`` ·
    ``keeper`` where ``keeper == (id == canonical_id)``).

    Shared by :func:`get_dedupe_groups` (the whole-list Dedupe workspace read) and the
    ``POST /duplicates/{hash}/undo`` router (phaze-be1j): undo must swap a restored group back
    into the live workspace using this SAME shell shape -- rendering the legacy
    ``group_card.html`` accordion row there left the toast's Undo unable to hand the restored
    group a working keeper-select card.
    """
    canonical_id = group["canonical_id"]
    files = group["files"]
    return {
        "sha256_hash": group["sha256_hash"],
        "group_name": Path(files[0]["original_path"]).name if files else group["sha256_hash"][:12],
        "count": len(files),
        "files": [
            {
                "id": f["id"],
                "name": Path(f["original_path"]).name,
                "quality": _format_quality(f),
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
        logger.warning("dedupe_groups_degraded", exc_info=True)
        return []


async def get_cue_review_cards(session: AsyncSession) -> list[dict[str, Any]]:
    """Return eligible + gated cue cards for the Cue preview workspace (degrade-safe, NO disk write).

    Surfaces two sets, both approved tracklists on an applied file (READ-05/D-01 -- an ``executed``
    ``RenameProposal`` exists, via ``applied_clause()``; the file's ``state`` column is NEVER read).
    WR-04: ``_MAX_REVIEW_ROWS`` is a PER-SET cap, not a single render budget -- the eligible and gated
    halves are each independently bounded by it, so the returned list holds up to ``2 *
    _MAX_REVIEW_ROWS`` cards (the intentional total ceiling, both halves SQL-bounded per WR-03):

    * **eligible** -- ``>= 1`` timestamped track (``_get_eligible_tracklist_query``). For each, the ``.cue``
      preview text is built ENTIRELY IN MEMORY via ``_build_cue_tracks`` + ``generate_cue_content`` -- the
      render NEVER calls ``write_cue_file`` and NEVER touches disk (T-60-CUE; the write happens only on an
      explicit APPROVE -> ``POST /cue/{id}/generate``, which IS the approve/write -- there is no /approve route).
    * **gated** -- approved + applied but NO timestamped track (the "awaiting tracklist match…" ineligible
      card, rendered ``opacity-60`` with no approve control).

    The whole read runs inside a ``session.begin_nested()`` SAVEPOINT and returns ``[]`` on any error so the
    render/poll path degrades instead of 500ing (no router try/except needed). Per card:
    ``tracklist_id`` · ``set_name`` (the audio file stem, matching the generated ``.cue`` name) ·
    ``eligible`` (bool) · ``cue_text`` (the in-memory ``.cue`` string, or ``None`` for a gated card).
    """
    try:
        async with session.begin_nested():
            cards: list[dict[str, Any]] = []

            # WR-03: bound the eligible half at the SQL level so the DB never returns more than the
            # render cap (the loop-break below no longer sits on top of a fully-materialized result).
            for tracklist, file_record in await _get_eligible_tracklist_query(session, limit=_MAX_REVIEW_ROWS):
                if len(cards) >= _MAX_REVIEW_ROWS:  # D-03: cap the eligible half at the same bound.
                    break
                cue_text: str | None = None
                if tracklist.latest_version_id:
                    cue_tracks = await _build_cue_tracks(session, tracklist.latest_version_id)
                    audio_name = Path(file_record.current_path).name
                    cue_text = generate_cue_content(audio_name, file_record.file_type, cue_tracks)
                cards.append(
                    {
                        "tracklist_id": tracklist.id,
                        "set_name": Path(file_record.current_path).stem,
                        "eligible": True,
                        "cue_text": cue_text,
                    }
                )

            # Gated: approved + applied() file but NO timestamped track (mirrors cue._get_cue_stats missing set).
            has_timestamp_subq = (
                select(TracklistVersion.tracklist_id)
                .join(TracklistTrack, TracklistTrack.version_id == TracklistVersion.id)
                .where(TracklistTrack.timestamp.is_not(None))
                .distinct()
            )
            gated_stmt = (
                select(Tracklist, FileRecord)
                .join(FileRecord, Tracklist.file_id == FileRecord.id)
                .where(
                    Tracklist.status == "approved",
                    Tracklist.file_id.is_not(None),
                    applied_clause(),
                    Tracklist.id.not_in(has_timestamp_subq),
                )
                .order_by(Tracklist.artist, Tracklist.event)
                .limit(_MAX_REVIEW_ROWS)  # WR-04: the gated half's own per-set cap (total ceiling = 2 * _MAX_REVIEW_ROWS)
            )
            for tracklist, file_record in (await session.execute(gated_stmt)).tuples().all():
                cards.append(
                    {
                        "tracklist_id": tracklist.id,
                        "set_name": Path(file_record.current_path).stem,
                        "eligible": False,
                        "cue_text": None,
                    }
                )

            return cards
    except Exception:
        logger.warning("cue_review_cards_degraded", exc_info=True)
        return []
