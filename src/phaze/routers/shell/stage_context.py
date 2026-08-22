"""Every per-stage context builder, and the ``stage -> builder`` dispatch map that indexes them.

Split out of the single-module ``routers/shell.py`` by phaze-bk9el.16. Each builder moved
VERBATIM -- same body, same signature, same docstring -- as did ``_STAGE_CONTEXT_BUILDERS``, whose
key set and key ORDER are pinned by the characterization golden.

``build_propose_list_context`` and ``build_changes_review_context`` are public because they have a
SECOND caller outside the shell (``proposals.bulk_action`` re-renders the same list after a bulk
action); the rest are private to the render path. They live here rather than in their own module
because they are stage contexts like the others -- ``propose`` and ``rename``/``move``/``tagwrite``
are their entries in the map below.

PATCH TARGETS MOVED WITH THE CODE. ``_metadata_stage_context`` resolves
``get_metadata_selection_summary`` and peers, and ``_dedupe_stage_context`` /
``_cue_stage_context`` resolve ``get_dedupe_groups`` / ``get_cue_review_cards``, through THIS
module's globals -- so a test that substitutes them patches
``phaze.routers.shell.stage_context.<name>``. A consequence of moving the functions, not a change
to them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode
import uuid

from sqlalchemy import func, select

from phaze.config import settings
from phaze.enums.stage import Stage, Status
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.routers.admin_agents import build_agents_pane_context
from phaze.routers.execution import build_audit_log_context
from phaze.routers.pipeline import FILES_SORT, build_dashboard_context
from phaze.routers.pipeline_scans import RECENT_SCANS_SORT, build_recent_scans
from phaze.routers.proposal_sort import PROPOSE_SORT
from phaze.routers.shell.stage_maps import _EMPTY_STATE_PARTIAL, CHANGES_LIST_CONTAINER_ID, PROPOSE_LIST_CONTAINER_ID, _stage_partial
from phaze.routers.shell.summary import _build_summary_context
from phaze.routers.view_state import PAGE_SIZE_CHOICES, ListViewState
from phaze.services.dedup import GROUP_PAGE_SIZE, count_duplicate_groups
from phaze.services.execution_preflight import get_execution_preflight
from phaze.services.pagination import DEFAULT_PAGE_SIZE, clamp_page, clamp_page_size
from phaze.services.pipeline import (
    ORPHANED_BUCKET,
    analyze_lanes_content_hash,
    count_proposal_pending_files,
    get_files_page,
    get_match_pending_tracklists,
    get_metadata_activity_summary,
    get_metadata_selection_summary,
    get_metadata_status_snapshot,
    get_stage_activity_snapshot,
    get_stage_progress,
)
from phaze.services.proposal_queries import get_proposal_stats
from phaze.services.review import (
    ChangesReviewStats,
    count_cue_review_candidates,
    dedupe_subcount_text,
    get_changes_review_page,
    get_cue_review_cards,
    get_dedupe_groups,
    get_proposal_workspace_page,
    get_tagwrite_review_page,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    # phaze-bk9el.16: type-only here. None of these builders is a FastAPI endpoint -- the two
    # route handlers stayed in the package __init__ -- so nothing resolves these annotations at
    # runtime and the TYPE_CHECKING move that CLAUDE.md warns about for Pydantic/FastAPI models
    # does not apply.
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession


async def _analyze_file_count(session: AsyncSession) -> int:
    """Return the total ``FileRecord`` count, degrade-safe (RECORD-04).

    A lightweight ``COUNT(*)`` read. On ANY error it returns a non-zero sentinel so a
    transient DB issue can NEVER falsely trip the first-run empty state (better to show
    the normal dashboard than to wrongly claim the archive is empty).

    The read runs inside a SAVEPOINT (``session.begin_nested()``), mirroring the CR-01 idiom
    :func:`phaze.services.pipeline._agent_stage_buckets` / :func:`~phaze.services.pipeline.get_agent_recent_scans`
    already document: ``_render_stage``'s analyze branch calls this AFTER ``build_dashboard_context``
    has loaded ``Agent`` / ``ScanBatch`` ORM rows into the SAME request session's identity map. A
    plain ``session.rollback()`` here would expire those already-loaded rows and 500 the subsequent
    Jinja render on the next lazy load (WR-05) -- exactly the DB hiccup this degrade path exists to
    survive. On error the nested scope is rolled back ALONE, recovering the aborted transaction
    without poisoning downstream reads on this same session.
    """
    try:
        async with session.begin_nested():
            result = await session.execute(select(func.count(FileRecord.id)))
    except Exception:
        # Broad by design (see docstring): ANY failure of this COUNT(*) -- driver error,
        # pool exhaustion, a transient network blip -- must degrade to "not empty" rather
        # than propagate, so a DB hiccup can never falsely trip the first-run empty state.
        return 1
    return int(result.scalar() or 0)


async def build_propose_list_context(request: Request, session: AsyncSession) -> dict[str, Any]:
    """Build every context key ``_propose_list.html`` needs, from ``request.query_params`` alone.

    Phase 60 (60-03, D-01): the Propose generation view over the shared RenameProposal source (NOT a
    diff). The Model column renders the CONFIGURED settings.llm_model (A1 -- one model per run, not a
    per-row value); a plain str off the module-level ControlSettings singleton (no DB, no enqueue).
    oob_counts stays False (Pitfall 5); the live sub-count rides the single chrome poll's OOB seeds.

    phaze-a6hm.2 / .9: the row read is the FILTERED + SEARCHED + PAGINATED
    ``get_proposal_workspace_page``, not the flat pending-only ``get_pending_proposal_rows``. Both
    emit the same row dict shape, so ``_file_table.html`` is unchanged by the swap. The display state
    comes from ``ListViewState.from_request`` -- the query string is the single source of truth for
    which slice is on screen, which is what makes the view bookmarkable, swap-stable and
    restore-correct in one move (see view_state.py). Defaults to ``status="pending"``: the
    workspace's job is the review queue, and landing on "all" would bury it under executed rows.

    phaze-a6hm.11 EXTRACTED this out of ``_render_stage`` so it has a SECOND caller:
    ``proposals.bulk_action``, which must re-render this exact list after a bulk approve/reject.
    That is the whole reason it is a function rather than an inline branch. A bulk action that
    rebuilt the context itself would be a second, independently-drifting description of what the
    container holds -- and "two producers of one container that disagree" is the phaze-7j50 defect
    this molecule already paid for once. Because BOTH callers derive the view from
    ``request.query_params`` through the same ``ListViewState.from_request``, the post-action
    re-render lands on the same filter/search/sort/page by construction, not by the bulk form
    remembering to restate six values (the phaze-gc5d guarantee, obtained structurally).

    The returned mapping is merged into the caller's base context; it is never a whole context on
    its own (it carries no ``request``/chrome keys).
    """
    view = ListViewState.from_request(request)
    # phaze-a6hm.10: the ONE resolution of this table's sort. `view.sort`/`view.order` are still
    # untrusted here -- ListViewState is a total PARSER, not a validator, so it will happily hand
    # back `sort="; DROP"` from a hand-edited URL. `resolve` is the gate: it matches by equality
    # against the enumerated keys and degrades anything else to `confidence`, so the string
    # cannot reach a column (column_sort rule 2) and does not 422 a render-path GET (rule 3).
    #
    # `sort_view_state()` is what makes the two contracts compose instead of compete. Header URLs
    # are spelled by SortState.url_for -- that is what _file_table.html calls, for all nine
    # workspaces that include it -- and this feeds it the status/search/page_size to preserve,
    # derived from ListViewState.params so the two can never enumerate different parameters. The
    # pager, tabs and search box keep spelling their own URLs with view.query(), which already
    # carries sort/order through, so a page change stays inside the operator's chosen order.
    sort_state = PROPOSE_SORT.resolve(sort=view.sort, order=view.order, view_state=view.sort_view_state())
    page = await get_proposal_workspace_page(
        session,
        status=view.status,
        search=view.q,
        page=view.page,
        page_size=view.page_size,
        sort=sort_state,
    )
    # phaze-1aybg: the GENERATE ALL confirm must quote the population the trigger actually
    # enqueues -- POST /pipeline/proposals batches ``get_proposal_pending_batches``'s convergence
    # set (files with metadata + completed analysis and NO proposal row yet), which is DISJOINT
    # from ``propose_stats.pending`` (RenameProposal rows already generated and awaiting review).
    # ``count_proposal_pending_files`` shares the exact predicate (`_proposal_pending_clauses`)
    # with the batching producer, so this count and the trigger's enqueue set can never drift
    # apart. Kept separate from ``propose_stats``, which stays the review-tab counts only.
    generation_pending = await count_proposal_pending_files(session)
    return {
        "propose_view": view,
        "sort": sort_state,
        "propose_proposals": page.rows,
        "propose_pagination": page.pagination,
        "propose_stats": page.stats,
        "propose_generation_pending": generation_pending,
        "propose_list_id": PROPOSE_LIST_CONTAINER_ID,
        # The pager's destination and page-size choices live in the BASE context (not a template
        # {% with %}) because _propose_list.html has three producers -- the full workspace render,
        # the bare fragment the router returns for a container-targeted swap, and the bulk response.
        # A value threaded in by only one of them would be missing on the others, and the pager
        # would render with empty hx-get URLs: controls that look fine and navigate nowhere.
        "pager_url": "/s/propose",
        "pager_target": f"#{PROPOSE_LIST_CONTAINER_ID}",
        "page_size_choices": PAGE_SIZE_CHOICES,
        "llm_model": settings.llm_model,
    }


def _parse_selected_ids(raw_value: str, limit: int = 100) -> list[str]:
    """The valid UUIDs in a comma-separated ``selected`` query value, capped at ``limit``.

    Unparseable entries are skipped rather than rejected -- the value round-trips through URLs the
    operator can edit, and one bad id should not discard a whole selection. The cap bounds what a
    hand-edited URL can push into the downstream IN () clause.
    """
    selected: list[str] = []
    for raw in raw_value.split(","):
        try:
            selected.append(str(uuid.UUID(raw)))
        except ValueError:
            continue
        if len(selected) >= limit:
            break
    return selected


async def build_changes_review_context(request: Request, session: AsyncSession) -> dict[str, Any]:
    """Build the canonical review list from URL-borne filter, page, and selection state."""
    view = ListViewState.from_request(request, status="needs_review", sort="confidence")
    allowed_statuses = {"all", "needs_review", "approved", "blocked", "rejected"}
    if view.status not in allowed_statuses:
        view = view.with_(status="needs_review")

    selected = _parse_selected_ids(request.query_params.get("selected", ""))

    proposals = await get_changes_review_page(
        session,
        status=view.status,
        page=view.page,
        page_size=view.page_size,
    )
    tag_page = await get_tagwrite_review_page(session)
    tag_rows = (
        [row for row in tag_page.rows if view.status == "all" or row["status"] == view.status]
        if view.status in {"all", "needs_review", "blocked"}
        else []
    )
    tag_needs_review = sum(row["status"] == "needs_review" for row in tag_page.rows)
    tag_blocked = sum(row["status"] == "blocked" for row in tag_page.rows)
    changes_stats = ChangesReviewStats(
        all=proposals.stats.all + len(tag_page.rows),
        needs_review=proposals.stats.needs_review + tag_needs_review,
        approved=proposals.stats.approved,
        blocked=proposals.stats.blocked + tag_blocked,
        rejected=proposals.stats.rejected,
    )

    def url_for(*, status: str | None = None, page: int | None = None, selected_ids: list[str] | None = None) -> str:
        params = view.params(status=status or view.status, page=page or view.page)
        params["selected"] = ",".join(selected if selected_ids is None else selected_ids)
        return f"/s/rename?{urlencode(params)}"

    return {
        "changes_view": view,
        "changes_proposals": proposals.rows,
        "changes_pagination": proposals.pagination,
        "changes_stats": changes_stats,
        "changes_selected": selected,
        "changes_tag_rows": tag_rows,
        "changes_tag_partial": tag_page.partial,
        "changes_urls": {one: url_for(status=one, page=1) for one in ("all", "needs_review", "approved", "blocked", "rejected")},
        "changes_prev_url": url_for(page=proposals.pagination.page - 1) if proposals.pagination.has_prev else None,
        "changes_next_url": url_for(page=proposals.pagination.page + 1) if proposals.pagination.has_next else None,
        "changes_list_id": CHANGES_LIST_CONTAINER_ID,
    }


async def _analyze_stage_context(request: Request, session: AsyncSession, stage: str) -> dict[str, Any]:
    """Build the Analyze stage's context update: the shared dashboard DAG content, the
    reload-safe selected-lane highlight, the poll content hash, and the first-run empty state.

    ``stage`` is passed in (never hardcoded to ``"analyze"``) so this stays correct if
    ``_STAGE_CONTEXT_BUILDERS`` ever aliases a second key at this function, the same way
    ``rename``/``move``/``tagwrite`` already alias at :func:`build_changes_review_context`.

    Phase 88 (88-01, DRILL-03 / D-02): a reload of /s/analyze?lane={id} seeds the selected-lane
    highlight server-side for the initial full grid (the poll re-applies it thereafter). Resolved
    by lookup-in-known-set against the seeded snapshot (T-88-01) -- an unknown/absent id highlights
    nothing, never errors.

    Phase 95 (phaze-zqvh.3): seed the #analyze-lanes content hash on the INITIAL render over the SAME
    inputs the /pipeline/stats poll hashes (lanes + selected highlight), so the first poll tick after
    an unchanged load is already a no-op OOB grid swap (the client htmx:oobBeforeSwap skip hook).

    Phase 61 (61-05, RECORD-04): first-run empty state. When NO files exist, swap the analyze
    stage_partial to the empty-state guide and inject the non-revoked agent list (for the
    agent-roots cards). file_count>0 leaves the dashboard render untouched; the fragment fork +
    oob_counts=False discipline stays intact (analyze_workspace.html is NOT edited -- the swap is
    purely via stage_partial).
    """
    update = dict(await build_dashboard_context(request.app.state, session))
    update["stage"] = stage
    update["stage_partial"] = _stage_partial(stage)
    update["oob_counts"] = False
    lane_param = request.query_params.get("lane")
    seeded_lanes = update.get("lanes") or []
    update["selected_lane"] = lane_param if any(one.get("id") == lane_param for one in seeded_lanes) else None
    update["lanes_hash"] = analyze_lanes_content_hash(seeded_lanes, update["selected_lane"])
    if await _analyze_file_count(session) == 0:
        update["stage_partial"] = _EMPTY_STATE_PARTIAL
        # SER-01: only kind="fileserver" agents host media and can be scan targets;
        # exclude kind="compute" (media-less burst backends) from the picker.
        agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
        update["agents"] = (await session.execute(agents_stmt)).scalars().all()
    return update


async def _files_stage_context(request: Request, session: AsyncSession, stage: str) -> dict[str, Any]:
    """Build the Files stage's context: the SAME default-first-page read the standalone GET
    /pipeline/files route builds (pipeline.pipeline_files) -- the bounded, per-page-derived,
    SAVEPOINT degrade-safe get_files_page with stage/bucket filters unset (the unfiltered
    overview; the _status_filter_bar in the partial drives filtering via /pipeline/files links).

    ``stage`` is passed in (never hardcoded to ``"files"``) for the same alias-robustness
    reason as :func:`_analyze_stage_context`.

    phaze-a6hm.3: this is the UNSORTED default landing, so resolve against no wire sort/order --
    reuses the SAME FILES_SORT contract instance pipeline.pipeline_files() resolves against
    (contract rule 6: one contract object per table), never a second one built here.
    """

    def query_int(name: str, default: int) -> int:
        try:
            return int(request.query_params.get(name, default))
        except (TypeError, ValueError):
            return default

    page = clamp_page(query_int("page", 1))
    page_size = clamp_page_size(query_int("page_size", DEFAULT_PAGE_SIZE))
    try:
        active_stage = Stage(request.query_params["stage"]) if request.query_params.get("stage") else None
    except ValueError:
        active_stage = None
    requested_bucket = request.query_params.get("bucket")
    valid_buckets = {status.value for status in Status} | {ORPHANED_BUCKET}
    active_bucket = requested_bucket if requested_bucket in valid_buckets else None
    files_sort_state = FILES_SORT.resolve(
        sort=request.query_params.get("sort"),
        order=request.query_params.get("order"),
        view_state={
            "page_size": page_size,
            "stage": active_stage.value if active_stage is not None else None,
            "bucket": active_bucket,
        },
    )
    files_page = await get_files_page(session, page=page, page_size=page_size, stage=active_stage, bucket=active_bucket, sort=files_sort_state)
    # phaze-t0b8: files_workspace.html now composes _workspace_scaffold.html like every other
    # STAGE_PARTIALS host, which both supplies the <h1 tabindex="-1"> focus target this stage was
    # missing and unconditionally includes _workspace_poll_seeds.html itself -- so the former
    # `include_poll_seeds` context flag (87-09 gap-fix's hand-rolled substitute for that same
    # include) has no remaining producer and is removed rather than left to double-emit the seeds.
    return {
        "files_page": files_page,
        "active_stage": active_stage.value if active_stage is not None else None,
        "active_bucket": active_bucket,
        "sort": files_sort_state,
        "stage": stage,
        "stage_partial": _stage_partial(stage),
        "oob_counts": False,
    }


async def _discover_stage_context(session: AsyncSession) -> dict[str, Any]:
    """Build the Discover stage's context: the existing recent-scans table (the SAME helper
    build_dashboard_context uses) plus the non-revoked agent list for the Trigger Scan form.

    phaze-8f9j: mounts the REAL recent_scans_table.html (delete control, failed-row
    error_message, stall indicator, sortable headers), so it needs that table's two context
    keys. `scans_poll=False` suppresses the partial's own 5s loop (WORK-05: one chrome poll);
    `poll=0` rides in the sort contract's view_state so a header click re-requests
    GET /pipeline/scans/recent WITH the flag. Resolved with sort=None/order=None because this
    is the FIRST render: header clicks go straight to pipeline_scans, never back through the
    shell, so there is no operator-chosen order to carry here yet.
    """
    scans_sort = RECENT_SCANS_SORT.resolve(sort=None, order=None, view_state={"poll": "0"})
    recent_scans = await build_recent_scans(session, sort=scans_sort)
    # SER-01: only kind="fileserver" agents host media and can be scan targets;
    # exclude kind="compute" (media-less burst backends) from the picker.
    agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
    agents = (await session.execute(agents_stmt)).scalars().all()
    return {"sort": scans_sort, "scans_poll": False, "recent_scans": recent_scans, "agents": agents}


async def _metadata_stage_context(session: AsyncSession) -> dict[str, Any]:
    """Build the Metadata stage's context.

    phaze-5462: deliberately carries NO file-list context. It used to seed `metadata_files`
    from get_metadata_pending_files, which is UNBOUNDED (no LIMIT, no ORDER BY) -- the same
    latent cliff that made the Analyze tab ship 12.7 MB. The workspace hx-gets the bounded
    GET /pipeline/pending-files fragment on load instead, so there is no file read on this path.
    """
    return {
        "metadata_activity": await get_metadata_activity_summary(session),
        "metadata_selection": await get_metadata_selection_summary(session),
        "metadata_status": await get_metadata_status_snapshot(session),
        "metadata_queue": await get_stage_activity_snapshot(session),
    }


async def _tracklist_stage_context(session: AsyncSession) -> dict[str, Any]:
    """Build the Tracklist stage's context: stage progress plus the MATCH ALL enqueue count.

    phaze-1wvb: the per-set table is NOT seeded here -- get_tracklist_set_rows was an unbounded
    row-per-Tracklist read rendered inline. The workspace hx-gets the bounded
    GET /pipeline/tracklist-sets fragment on load instead. `tracklist_match_pending` stays as
    it is: it feeds the MATCH ALL *enqueue* set (paging contract rule 7), never a render.
    """
    return {
        "tracklist_steps": await get_stage_progress(session),
        "tracklist_match_pending": len(await get_match_pending_tracklists(session)),
    }


async def _dedupe_stage_context(session: AsyncSession) -> dict[str, Any]:
    """The Dedupe stage's scored duplicate groups (each keeper == score_group's canonical_id).

    get_dedupe_groups is a read-only, SAVEPOINT-wrapped, degrade-safe assembly over the
    existing dedup reads (NO new query path, NO enqueue, NO backend change) that returns []
    on any DB error, so no router try/except is needed.

    phaze-4iq5t: also carries ``dedupe_total_groups`` (one ``count_duplicate_groups`` corpus-wide
    COUNT -- the SAME call the Apply-stage preflight below already pays every render, so this is one
    additional scan per Dedupe-stage VISIT, not a per-page-render cost the general paging contract's
    rule 2 (services/pagination.py) forbids) and ``dedupe_page_size`` (GROUP_PAGE_SIZE), so the
    template can render an honest "Showing N of M" subcount and a bounded "Load more" affordance
    instead of silently rendering only the first page as though it were everything.
    """
    groups = await get_dedupe_groups(session)
    total = await count_duplicate_groups(session)
    return {
        "dedupe_groups": groups,
        "dedupe_total_groups": total,
        "dedupe_page_size": GROUP_PAGE_SIZE,
        "dedupe_subcount": dedupe_subcount_text(len(groups), total),
    }


async def _cue_stage_context(session: AsyncSession) -> dict[str, Any]:
    """The Cue stage's eligible + gated preview cards, built IN MEMORY (no disk write).

    get_cue_review_cards is a read-only, SAVEPOINT-wrapped, degrade-safe assembly over the
    existing cue reads (NO write_cue_file, NO enqueue, NO backend change) that returns [] on
    any DB error, so no router try/except is needed.
    """
    return {"cue_cards": await get_cue_review_cards(session)}


async def _apply_stage_context(session: AsyncSession) -> dict[str, Any]:
    """Build the Apply (Execute) stage's context: aggregate proposal counts plus the full
    execution preflight manifest.

    phaze-tzy6s.12 / D-10: get_execution_preflight deliberately reuses start_execution's OWN
    reads so the manifest cannot drift from the dispatch it describes.

    phaze-tzy6s.17: the three adjacent counts (dedupe/cue/tagwrite) are real COUNT queries, not
    `len()` of the three list readers -- those saturate silently at their page caps, and doing
    the full list-building work just to keep `len()` was wasted work on the one screen that
    must not be wrong. Tag writes get `partial` instead of a COUNT because eligibility there is
    a Python predicate (compute_proposed_tags per candidate); TagwriteReviewPage.partial already
    means "row cap hit with the candidate set not provably exhausted" (phaze-a2ytu).
    """
    stats = await get_proposal_stats(session)
    tagwrite_page = await get_tagwrite_review_page(session)
    preflight = await get_execution_preflight(
        session,
        pending=stats.pending,
        rejected=stats.rejected,
        tagwrite_pending=len(tagwrite_page.rows),
        tagwrite_pending_at_least=tagwrite_page.partial,
        dedupe_pending=await count_duplicate_groups(session),
        cue_pending=await count_cue_review_candidates(session),
    )
    return {"stats": stats, "preflight": preflight}


async def _audit_stage_context(request: Request, session: AsyncSession) -> dict[str, Any]:
    """Build the Audit utility pane's context (UTILITY_PANES, not a DAG pipeline stage).

    phaze-uvmcr.3: ``execution.audit_log``'s non-fragment branch redirects a plain request /
    bookmark / history-restore of GET /audit/ HERE, carrying the request's whole query string,
    so the status/page/page_size/sort/order it arrives with must resolve to the SAME filtered
    view on this side. Parsed off ``request.query_params`` and clamped with the SAME
    ``phaze.services.pagination`` helpers every other paged read in this router composes, so an
    absurd/unparseable value degrades to the same safe default everywhere else (never a 422 on
    a render path). The context comes from :func:`build_audit_log_context`, the SAME function
    the GET /audit/ fragment endpoint calls, so the two producers cannot independently drift.
    """
    audit_params = request.query_params
    try:
        audit_page_num = clamp_page(int(audit_params.get("page", "1")))
    except ValueError:
        audit_page_num = clamp_page(1)
    try:
        audit_page_size = clamp_page_size(int(audit_params.get("page_size", str(DEFAULT_PAGE_SIZE))))
    except ValueError:
        audit_page_size = clamp_page_size(DEFAULT_PAGE_SIZE)
    return await build_audit_log_context(
        session,
        status=audit_params.get("status"),
        page=audit_page_num,
        page_size=audit_page_size,
        sort=audit_params.get("sort"),
        order=audit_params.get("order"),
    )


async def _agents_stage_context(request: Request, session: AsyncSession, stage: str) -> dict[str, Any]:
    """Build the Agents/Compute utility pane's context (UTILITY_PANES, below-the-line).

    ``stage`` is passed in (never hardcoded to ``"agents"``) for the same alias-robustness
    reason as :func:`_analyze_stage_context`.

    phaze-uvmcr.4: context comes from admin_agents.build_agents_pane_context, the SAME assembly
    GET /admin/agents's redirect target uses -- shared so this pane and the (now-redirecting)
    legacy route can never diverge on what a render needs. That function reads
    ?agent=/?clane=/?sort=/?order= straight off request.query_params (mirroring the ``analyze``
    stage's ?lane= resolution) rather than declaring typed Query() params on THIS route, so the
    wire-bounds contract stays scoped to the routes that actually declare them.
    """
    update = dict(await build_agents_pane_context(request, session))
    update["stage"] = stage
    update["stage_partial"] = _stage_partial(stage)
    update["oob_counts"] = False
    return update


# stage -> async context-update builder, dispatched by _render_stage. Every value has the
# signature (request, session, stage) -> dict[str, Any] -- `stage` is always the SAME string
# used to key this dict entry (T-57-01: never used to build a template path itself), passed
# through explicitly rather than assumed by the callee, so a builder stays correct if a second
# key is ever aliased at it -- exactly like `rename`/`move`/`tagwrite` already alias at
# build_changes_review_context below, which is stage-agnostic and ignores the argument. A stage
# absent from this map (only "operations" today) keeps the base context untouched, matching the
# pre-decomposition behavior of an if/elif chain with no matching branch.
_STAGE_CONTEXT_BUILDERS: dict[str, Callable[[Request, AsyncSession, str], Awaitable[dict[str, Any]]]] = {
    "summary": lambda request, session, _stage: _build_summary_context(request.app.state, session),
    "analyze": _analyze_stage_context,
    "files": _files_stage_context,
    "discover": lambda _request, session, _stage: _discover_stage_context(session),
    "metadata": lambda _request, session, _stage: _metadata_stage_context(session),
    "tracklist": lambda _request, session, _stage: _tracklist_stage_context(session),
    "rename": lambda request, session, _stage: build_changes_review_context(request, session),
    "move": lambda request, session, _stage: build_changes_review_context(request, session),
    "tagwrite": lambda request, session, _stage: build_changes_review_context(request, session),
    "propose": lambda request, session, _stage: build_propose_list_context(request, session),
    "dedupe": lambda _request, session, _stage: _dedupe_stage_context(session),
    "cue": lambda _request, session, _stage: _cue_stage_context(session),
    "apply": lambda _request, session, _stage: _apply_stage_context(session),
    "audit": lambda request, session, _stage: _audit_stage_context(request, session),
    "agents": _agents_stage_context,
}
