"""v7.0 shell router -- owns ``GET /`` (Summary default) and ``GET /s/{stage}``.

This is the load-bearing spine of the v7.0 three-column "Hybrid Console" shell
(Phase 57). It serves the structural shell (header · DAG rail · ``#stage-workspace`` ·
right pane) on a direct/bookmark navigation, and a bare content fragment on an HTMX
rail swap -- the fork decided by ``response_shape.wants_fragment`` (contract rule 1),
the same predicate ``admin_agents.page`` (``routers/admin_agents.py``) composes.

Stage resolution is a strict whitelist across TWO static maps: ``STAGE_PARTIALS`` for the DAG
pipeline rail nodes (D-01), and its sibling ``UTILITY_PANES`` (phaze-uvmcr.1) for the two
below-the-line utility panes -- Audit Log and Compute/Agents -- that are NOT DAG stages and so
are kept out of ``STAGE_PARTIALS``, whose own docstring pins its key set AND order verbatim to
the 57-UI-SPEC "DAG Rail" table. ``stage`` is NEVER interpolated into a template path -- the
partial name always comes from one of these two static dicts, closing the
template-path-injection surface (T-57-01 / ASVS V5). An unknown stage (in neither map) 404s
(D-02).

``GET /`` renders the Summary landing placeholder (quick 260707-sq3) -- a static, DB-free
stage reserving the landing slot for a future at-a-glance overview. Analyze is one rail click
away at ``/s/analyze``, where it still embeds the existing pipeline-dashboard content; its
context is built by the shared ``build_dashboard_context`` factored out of
``pipeline.dashboard()`` so the two paths cannot drift (D-01 / RESEARCH Open-Q2). The
remaining nodes render a minimal placeholder in Phase 57 -- their rich workspaces (and live
content bridges) land with their workspaces in Phases 58-61.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from phaze.config import settings
from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import APPROVE_REJECT_FROM
from phaze.routers.pipeline import FILES_SORT
from phaze.routers.pipeline_scans import RECENT_SCANS_SORT, build_recent_scans
from phaze.routers.proposal_sort import PROPOSE_SORT
from phaze.routers.response_shape import wants_fragment
from phaze.routers.view_state import PAGE_SIZE_CHOICES, ListViewState
from phaze.services.pipeline import (
    analyze_lanes_content_hash,
    count_proposal_pending_files,
    get_files_page,
    get_match_pending_tracklists,
    get_stage_progress,
)
from phaze.services.proposal_queries import get_proposal_stats
from phaze.services.review import (
    get_cue_review_cards,
    get_dedupe_groups,
    get_pending_proposal_rows,
    get_proposal_workspace_page,
    get_tagwrite_review_page,
)
from phaze.services.route_control import get_route_control
from phaze.web.static import static_asset_url

from .pipeline import build_dashboard_context


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# phaze-315t: fingerprinted, cache-forever static asset URLs. `shell/shell.html` is the
# top-level layout that carries the app.css link + favicon set.
templates.env.globals["static_url"] = static_asset_url
router = APIRouter(tags=["shell"])

# Rail-node id -> bridged content partial (D-01). The keys + their order are VERBATIM
# from the prototype RAIL config (57-UI-SPEC "DAG Rail" table); every node now resolves
# to its redesigned per-stage workspace (Phases 58-61). Every VALUE is a STATIC string
# literal: `stage` is matched against these keys and never spliced into a template path
# (T-57-01 -- template-path-injection mitigation). The literals also act as the
# dead-template guard's entry roots, so each stays reachable.
STAGE_PARTIALS: dict[str, str] = {
    # Quick 260707-sq3 (SQ3-01): the `/` landing placeholder. FIRST key so the dict order matches
    # the rail order. A STATIC string literal (T-57-01: `stage` is never spliced into a template
    # path) that also acts as the dead-template guard's entry root. The stage has NO DB-backed
    # context -- `_render_stage` deliberately gives it no branch (zero reads, zero extra keys).
    "summary": "shell/partials/summary_placeholder.html",
    # Phase 87 (87-09, UI-01/UI-02): the derived per-file stage-matrix files page -- the scannable
    # "where's this file at?" overview -- surfaced as a first-class, reachable rail workspace. Its
    # backing route GET /pipeline/files (pipeline.py) rendered this same partial but was UNREACHABLE
    # from the shell (no nav entry, no full-page fork); this second reference makes it a real stage.
    # A STATIC string literal (T-57-01: `stage` is NEVER spliced into a template path) that also
    # doubles as a dead-template-guard entry root. Placed right after summary -- the file-level
    # overview sibling of the stage-level Summary landing.
    # phaze-mrhq: points at the thin HOST wrapper (files_workspace.html), not files_table_view.html
    # directly -- mirrors the analyze_workspace.html / _analyze_files.html host+fragment split
    # (phaze-5462). files_table_view.html is the swap-target FRAGMENT every filter/sort/pager click
    # re-fetches from GET /pipeline/files; it carries no id of its own, so the id-bearing host div
    # here is never nested inside itself.
    "files": "pipeline/partials/files_workspace.html",
    # Phase 58 (58-02, WORK-01): the first real workspace -- a static literal (T-57-01: `stage`
    # is never spliced into a template path). Supersedes-in-place; legacy templates stay until CUT-02.
    "discover": "pipeline/partials/discover_workspace.html",
    # Phase 58 (58-03, WORK-02): the Metadata enrich workspace -- a static literal
    # (T-57-01: `stage` is never spliced into a template path). Supersede-in-place; legacy templates
    # stay until CUT-02.
    "metadata": "pipeline/partials/metadata_workspace.html",
    # Phase 58 (58-04, WORK-03/04): the real Analyze workspace (3 lane cards + reused cloud cards +
    # per-file lane/window table) supersedes the bridged dag_canvas.html -- a static literal (T-57-01:
    # `stage` is never spliced into a template path). dag_canvas.html stays reachable via the legacy
    # dashboard.html until CUT-02 (Phase 62), so the dead-template guard stays green (supersede-in-place).
    "analyze": "pipeline/partials/analyze_workspace.html",
    # Phase 59 (59-03, IDENT-02): the real Tracklist workspace (three Search/Scrape/Match step cards
    # with per-step ALL triggers over the existing bulk endpoints + a per-set N/M track-coverage
    # table) supersedes the placeholder -- a STATIC string literal (T-57-01: `stage` is never spliced
    # into a template path). Supersede-in-place; the legacy template stays reachable until CUT-02.
    "tracklist": "pipeline/partials/tracklist_workspace.html",
    # Phase 60 (60-03, D-01): the real Propose generation view (the pending RenameProposal list + Model +
    # Conf + a GENERATE ALL trigger over the existing POST /pipeline/proposals) supersedes the placeholder
    # -- a STATIC string literal (T-57-01: `stage` is never spliced into a template path). Supersede-in-place.
    "propose": "pipeline/partials/propose_workspace.html",
    # Phase 60 (60-02, REVIEW-01/REVIEW-02): the real Rename/Path + Move-files review diff workspaces
    # (the ONE shared _diff_row.html over pending RenameProposal rows -- filename facet vs proposed_path
    # facet, D-06) supersede the placeholders -- STATIC string literals (T-57-01: `stage` is never
    # spliced into a template path). Supersede-in-place; the legacy templates stay reachable until CUT-02.
    "rename": "pipeline/partials/rename_workspace.html",
    # Phase 60 (60-03, REVIEW-01/REVIEW-02): the real Tag-write review workspace (the shared _diff_row.html
    # over the computed tag comparison -- APPROVE POSTs /tags/{id}/write, bulk POSTs the D-03 server-predicate
    # /tags/bulk-write-no-discrepancies) supersedes the placeholder -- a STATIC string literal (T-57-01).
    "tagwrite": "pipeline/partials/tagwrite_workspace.html",
    "move": "pipeline/partials/move_workspace.html",
    # Phase 60 (60-04, REVIEW-03/REVIEW-05): the real Dedupe keeper-select workspace (duplicate-group
    # cards + a keeper radio wired to the VERIFIED /duplicates/{sha256_hash}/resolve contract + page-scoped
    # AUTO-KEEP + the file_states undo round-trip) supersedes the placeholder -- a STATIC string literal
    # (T-57-01: `stage` is never spliced into a template path). Supersede-in-place; legacy templates stay.
    "dedupe": "pipeline/partials/dedupe_workspace.html",
    # Phase 60 (60-04, REVIEW-04): the real Cue preview workspace (in-memory .cue preview cards + an
    # APPROVE wired to /cue/{id}/generate + visibly gated ineligible cards) supersedes the placeholder --
    # a STATIC string literal (T-57-01). This is the LAST of the six Review workspaces; every placeholder
    # is now superseded. Supersede-in-place; the legacy template stays reachable until CUT-02 (Phase 62).
    "cue": "pipeline/partials/cue_workspace.html",
    # phaze-vvmh: the Apply (Execute) stage -- the terminal node of "nothing moves without review,
    # then execute", and the ONLY live caller of POST /execution/start. Its predecessor, the Execute
    # Approved button in proposals/partials/stats_bar.html, rode inside an OOB fragment addressed to
    # `#stats-bar`, an id the Phase-62 cutover deleted, so no served document has contained an
    # execute trigger since. Approved proposals accumulated with no way to dispatch them from the UI.
    # A STATIC string literal (T-57-01: `stage` is never spliced into a template path) that also acts
    # as a dead-template-guard entry root. LAST key so the dict order matches the rail order: it
    # closes the Review & Apply group, after the five review nodes it consumes the output of.
    "apply": "pipeline/partials/apply_workspace.html",
}


# phaze-uvmcr.1: rail-node id -> bridged content partial for the two BELOW-THE-LINE utility
# panes (Audit Log, Compute/Agents) -- the sibling of STAGE_PARTIALS above, deliberately kept
# SEPARATE from it rather than folded in. STAGE_PARTIALS' own comment pins its key set AND
# order VERBATIM to the 57-UI-SPEC "DAG Rail" table; Audit and Agents are not DAG pipeline
# stages -- they sit below the rail's border-t divider, carry no pipeline count/badge and never
# take the blue aria-[current=page] active tint reserved for pipeline nodes (rail.html) -- so
# adding them to STAGE_PARTIALS would silently falsify a documented invariant instead of
# widening it honestly. This map exists so that claim stays literally true.
#
# Same T-57-01 discipline as STAGE_PARTIALS: every VALUE is a STATIC string literal, `stage` is
# matched against these keys and NEVER spliced into a template path, and the literals double as
# dead-template-guard entry roots (test_dead_template_guard.py) exactly like STAGE_PARTIALS'.
#
# phaze-uvmcr.1 lands both keys with placeholder/thin content so this bead is independently
# mergeable; phaze-uvmcr.3 (audit) and phaze-uvmcr.4 (agents) replace the values with the real
# hosted panes once their content has settled.
UTILITY_PANES: dict[str, str] = {
    "audit": "shell/partials/audit_placeholder.html",
    "agents": "shell/partials/agents_placeholder.html",
}


def _stage_partial(stage: str) -> str:
    """Resolve ``stage`` to its content partial across both static maps (T-57-01).

    Checked in ``STAGE_PARTIALS`` first (DAG rail stages), then ``UTILITY_PANES``
    (below-the-line utility panes). Both are STATIC string-literal dicts keyed by ``stage`` --
    never spliced into a template path -- so this preserves T-57-01 whichever map resolves it.
    Callers are expected to have already validated ``stage in STAGE_PARTIALS or stage in
    UTILITY_PANES`` (``shell_stage`` does; ``shell_home`` passes the hardcoded ``"summary"``
    literal); a stage in neither raises ``KeyError``, matching the pre-existing
    ``STAGE_PARTIALS[stage]`` behavior this replaces.
    """
    return STAGE_PARTIALS.get(stage) or UTILITY_PANES[stage]


# Phase 61 (61-05, RECORD-04): the first-run empty-state guide. A STATIC string literal
# (T-57-01: `stage` is never spliced into a template path) the analyze render swaps `stage_partial`
# to when the file count is exactly 0. The guide lists each agent's already-configured `scan_roots`
# and posts the DISCOVERY scan (POST /pipeline/scans) — zero new input surface (D-08).
_EMPTY_STATE_PARTIAL = "pipeline/partials/empty_state.html"

# phaze-a6hm.2 / .9: the id of the Propose workspace's list container -- the swap target every filter
# tab, search keystroke and pager click aims at. Spelled ONCE, here, and injected into the template
# context rather than hardcoded in markup, so the router's HX-Target comparison and the element that
# must match it cannot drift apart.
#
# ID UNIQUENESS (argued, not assumed -- this repo has FOUR duplicate-id OOB bugs on record: gzrd,
# op6f, 7j50, and the one 5p43 avoided):
#
# 1. It is NOT "proposal-list-container". That id belongs to the legacy proposals view and is
#    contractually defined by proposals/partials/proposal_list.html as holding exactly
#    `proposal_table + bulk_actions + pagination`. This container holds a _file_table-based workspace
#    list instead, so reusing the id would create a SECOND, DISAGREEING definition of the same id --
#    precisely the phaze-7j50 defect. The names are deliberately more than one character apart
#    ("propose-workspace-list" vs "proposal-list-container") so neither reads as a typo of the other.
# 2. It cannot collide within the propose render: the id is emitted by exactly one element, in
#    _propose_list_host, which is included exactly once by propose_workspace.html.
# 3. It cannot collide ACROSS stages: STAGE_PARTIALS maps one partial per stage and only the propose
#    workspace includes that host, and #stage-workspace holds exactly one stage at a time.
# 4. It cannot be duplicated BY ITS OWN SWAPS -- the recurring shape of the four bugs above, where a
#    fragment re-emits its own wrapper and nests a copy inside itself. The narrow-swap branch in
#    _render_stage returns _propose_list.html (the container's INNER content), never the host div, and
#    the two files are kept separate for exactly that reason. The full-workspace branch is the only
#    producer of the wrapper. phaze-a6hm.11 added a THIRD producer -- the bulk approve/reject
#    response -- and it obeys the same split: _propose_bulk_response.html includes _propose_list.html
#    (inner content) and never the wrapper, so a bulk action cannot nest a second container either.
#    The bulk controls introduce NO new id of their own: they live inside this container, and they
#    address the checkboxes through a descendant selector rooted at THIS id rather than a private id
#    of their own, so there is nothing new here that could collide with anything.
# 5. No OOB fragment targets it: this container is only ever an hx-target, and oob_counts stays False
#    on every stage render (Pitfall 5), so the chrome poll's OOB seeds cannot land here.
PROPOSE_LIST_CONTAINER_ID = "propose-workspace-list"


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
    # phaze-a6hm.11 selection metadata. `row_select_locked` is computed HERE, from the same
    # APPROVE_REJECT_FROM the router enforces on the write, so the greyed-out checkbox and the
    # server's guard cannot drift into disagreeing about which rows may transition. It is an
    # affordance only: the server re-checks every id it is sent regardless (request_guards rule 2 --
    # the browser's id-set is always assumed stale), which is why a row that goes terminal between
    # this render and the submit is still correctly SKIPPED rather than rewritten.
    select_ids = [str(row["id"]) for row in page.rows]
    select_locked = [row["status"] not in APPROVE_REJECT_FROM for row in page.rows]
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
        "row_select_ids": select_ids,
        "row_select_locked": select_locked,
        "select_name": "proposal_ids",
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


async def _render_stage(request: Request, stage: str, session: AsyncSession) -> HTMLResponse:
    """Render ``stage`` as the full shell (direct nav) or a bare fragment (HX rail swap).

    The fork is ``response_shape.wants_fragment`` (contract rule 1 -- the ONLY sanctioned
    way to ask): a LIVE htmx rail swap gets the content-only ``shell/_stage_fragment.html``
    (which NEVER extends ``base.html`` -- a fragment carrying ``<html>``/``<head>``
    corrupts the shell, a ROADMAP-locked anti-pattern); a direct navigation, a bookmark,
    OR A HISTORY RESTORE gets the full ``shell/shell.html``
    chrome.

    That last shape is why this is not the raw ``HX-Request`` check it used to be
    (phaze-64uy). Every rail node in ``shell/partials/rail.html`` carries
    ``hx-get="/s/<stage>" hx-target="#stage-workspace" hx-push-url="true"``, so EVERY stage
    the operator visits pushes a ``/s/*`` URL into history. Press Back with that snapshot
    evicted from htmx's 10-entry ``historyCacheSize`` (routine -- a fresh session or cleared
    ``localStorage`` does it too) and htmx re-fetches the URL as a restore carrying BOTH
    ``HX-Request: true`` and ``HX-History-Restore-Request: true``. On a restore htmx IGNORES
    ``hx-target`` and swaps the response into ``<body>`` (nothing here carries
    ``[hx-history-elt]``), so the old raw check answered with ``_stage_fragment.html`` and
    DESTROYED the rail, header, palette launcher and status strip -- leaving a bare workspace
    with no navigation and no way out but a manual reload. Reachable from every stage in the
    app, which is what made this the worst instance of the class. ``oob_counts=False`` so the initial render never emits the ``hx-swap-oob``
    "files ready" paragraphs (Pitfall 5 -- they would collide on duplicate ids with the
    DAG canvas seeds; they are honored only during a real ``/pipeline/stats`` swap).

    Only the Analyze node needs DB-backed context -- it embeds the live pipeline-dashboard
    DAG content via the shared :func:`build_dashboard_context`. The shell context keys
    (``stage`` / ``stage_partial`` / ``oob_counts``) are re-asserted AFTER the dashboard
    context merge so the bridged context can never shadow them.
    """
    context: dict[str, Any] = {
        "request": request,
        "stage": stage,
        "stage_partial": _stage_partial(stage),
        "oob_counts": False,
        # Phase 71 (71-04, BEUI-02): seed the header force-local pill's state on EVERY page from the
        # durable route_control 'global' row (get_route_control is degrade-safe -> False on any DB
        # error, never raises). Seeded HERE in the base shell context -- NOT the Analyze-only
        # build_dashboard_context -- so the global incident control shows correct state everywhere.
        "force_local": await get_route_control(session),
    }
    if stage == "analyze":
        context.update(await build_dashboard_context(request.app.state, session))
        context["stage"] = stage
        context["stage_partial"] = _stage_partial(stage)
        context["oob_counts"] = False
        # Phase 88 (88-01, DRILL-03 / D-02): a reload of /s/analyze?lane={id} seeds the selected-lane
        # highlight server-side for the initial full grid (the poll re-applies it thereafter). Resolved
        # by lookup-in-known-set against the seeded snapshot (T-88-01) — an unknown/absent id highlights
        # nothing, never errors.
        lane_param = request.query_params.get("lane")
        seeded_lanes = context.get("lanes") or []
        context["selected_lane"] = lane_param if any(one.get("id") == lane_param for one in seeded_lanes) else None
        # Phase 95 (phaze-zqvh.3): seed the #analyze-lanes content hash on the INITIAL render over the SAME
        # inputs the /pipeline/stats poll hashes (lanes + selected highlight), so the first poll tick after
        # an unchanged load is already a no-op OOB grid swap (the client htmx:oobBeforeSwap skip hook).
        context["lanes_hash"] = analyze_lanes_content_hash(seeded_lanes, context["selected_lane"])
        # Phase 61 (61-05, RECORD-04): first-run empty state. When NO files exist, swap the
        # analyze stage_partial to the empty-state guide and inject the non-revoked agent list
        # (for the agent-roots cards). file_count>0 leaves the dashboard render untouched; the
        # fragment fork + oob_counts=False discipline stays intact (analyze_workspace.html is NOT
        # edited — the swap is purely via stage_partial).
        if await _analyze_file_count(session) == 0:
            context["stage_partial"] = _EMPTY_STATE_PARTIAL
            # SER-01: only kind="fileserver" agents host media and can be scan targets;
            # exclude kind="compute" (media-less burst backends) from the picker.
            agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
            context["agents"] = (await session.execute(agents_stmt)).scalars().all()
    elif stage == "files":
        # Phase 87 (87-09, UI-01/UI-02): the derived per-file stage-matrix files page, surfaced as a
        # reachable rail workspace. Build the SAME context the standalone GET /pipeline/files route
        # does (pipeline.pipeline_files): the bounded, per-page-derived, SAVEPOINT degrade-safe
        # get_files_page over the default first page (stage/bucket filters are UNSET here -- the
        # unfiltered overview; the _status_filter_bar in the partial drives filtering via
        # /pipeline/files links). The four keys mirror the route verbatim (phaze-a6hm.3 added `sort`).
        # stage/stage_partial/oob_counts are re-asserted AFTER (defensive; the merge above only added
        # base keys) so the files context can never shadow the shell fork discriminators.
        # phaze-a6hm.3: this is the UNSORTED default landing, so resolve against no wire sort/order --
        # reuses the SAME FILES_SORT contract instance pipeline.pipeline_files() resolves against
        # (contract rule 6: one contract object per table), never a second one built here.
        files_sort_state = FILES_SORT.resolve(sort=None, order=None, view_state={"page_size": 25, "stage": None, "bucket": None})
        context["files_page"] = await get_files_page(session, page=1, page_size=25, stage=None, bucket=None, sort=files_sort_state)
        context["active_stage"] = None
        context["active_bucket"] = None
        context["sort"] = files_sort_state
        # phaze-t0b8: files_workspace.html now composes _workspace_scaffold.html like every other
        # STAGE_PARTIALS host, which both supplies the <h1 tabindex="-1"> focus target this stage was
        # missing and unconditionally includes _workspace_poll_seeds.html itself — so the former
        # `include_poll_seeds` context flag (87-09 gap-fix's hand-rolled substitute for that same
        # include) has no remaining producer and is removed rather than left to double-emit the seeds.
        context["stage"] = stage
        context["stage_partial"] = _stage_partial(stage)
        context["oob_counts"] = False
    elif stage == "discover":
        # Phase 58 (58-02, WORK-01): the Discover workspace reuses the EXISTING recent-scans
        # data verbatim (build_recent_scans -- the SAME helper build_dashboard_context uses) and
        # the non-revoked agent list driving the reused Trigger Scan form. Both reads degrade-safe
        # at the service/ORM layer (no router try/except). oob_counts stays False on the stage
        # render (Pitfall 3); the live sub-count refreshes via the single chrome poll's OOB seeds.
        # phaze-8f9j: the workspace mounts the REAL recent_scans_table.html now (delete control,
        # failed-row error_message, stall indicator, sortable headers), so it needs that table's two
        # context keys. `scans_poll=False` suppresses the partial's own 5s loop (WORK-05: one chrome
        # poll), and `poll=0` rides in the sort contract's view_state so a header click re-requests
        # GET /pipeline/scans/recent WITH the flag -- otherwise the copy swapped in by the re-sort
        # would arm the loop the mount exists to avoid. Resolved with sort=None/order=None because
        # this is the FIRST render: header clicks go straight to pipeline_scans, never back through
        # the shell, so there is no operator-chosen order to carry here yet.
        scans_sort = RECENT_SCANS_SORT.resolve(sort=None, order=None, view_state={"poll": "0"})
        context["sort"] = scans_sort
        context["scans_poll"] = False
        context["recent_scans"] = await build_recent_scans(session, sort=scans_sort)
        # SER-01: only kind="fileserver" agents host media and can be scan targets;
        # exclude kind="compute" (media-less burst backends) from the picker.
        agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
        context["agents"] = (await session.execute(agents_stmt)).scalars().all()
    # phaze-5462: the metadata stage deliberately gets NO file-list context here any more. It used
    # to seed `metadata_files` from get_metadata_pending_files, which is UNBOUNDED (no LIMIT, no
    # ORDER BY) -- the same latent cliff that made the Analyze tab ship 12.7 MB. That tab measured a
    # harmless ~70 KB only because its backlog happens to be empty in production today, NOT because
    # it was paged. The workspace now hx-gets the bounded GET /pipeline/pending-files fragment on
    # load instead, so there is no file read on this path.
    elif stage == "tracklist":
        # Phase 59 (59-03, IDENT-02), reworked by phaze-2akf: the Tracklist workspace renders the
        # drain panel (its own hx-get fragment, phaze-fq9h.8) as the LOOKUP stage, plus the MATCH
        # step card over the existing bulk trigger, plus the per-set N/M coverage table (also
        # hx-get). The former SEARCH and SCRAPE step cards are gone with the tasks behind them --
        # see the template for why those two stages stopped describing anything real. What is left
        # here is read-only and degrade-safe over the existing tracklist reads (NO new query path,
        # NO enqueue). The match busy pill binds to the existing matchBusy store key (Pitfall 3 --
        # no new key, no second poll), so oob_counts stays False (Pitfall 5).
        context["tracklist_steps"] = await get_stage_progress(session)
        context["tracklist_match_pending"] = len(await get_match_pending_tracklists(session))
        # phaze-1wvb: the per-set table is NOT seeded here -- get_tracklist_set_rows was an
        # unbounded row-per-Tracklist read rendered inline. The workspace hx-gets the bounded
        # GET /pipeline/tracklist-sets fragment on load instead. NOTE tracklist_match_pending stays
        # as it is: it feeds the MATCH ALL *enqueue* set (paging contract rule 7), never a render.
    elif stage == "rename":
        # Phase 60 (60-02, REVIEW-01/REVIEW-02): the Rename/Path review workspace renders the pending
        # RenameProposal rows (filename facet) through the shared _diff_row.html. get_pending_proposal_rows
        # is a read-only, SAVEPOINT-wrapped, degrade-safe assembly over the existing proposal reads (NO
        # new query path, NO enqueue, NO backend change) that degrades to empty/zero on any DB error, so
        # no router try/except is needed; oob_counts stays False (Pitfall 5) -- the live sub-count would
        # ride the single chrome poll's OOB seeds.
        #
        # phaze-rw14: the row list is capped at 200 for render; the header/confirm counts below are the
        # bundled REAL totals (corpus-wide pending count, >=90%-confidence pending count), not the
        # capped list's length.
        rename_pending = await get_pending_proposal_rows(session)
        context["rename_proposals"] = rename_pending.rows
        context["rename_pending_total"] = rename_pending.total_pending
        context["rename_high_confidence_pending"] = rename_pending.high_confidence_pending
    elif stage == "move":
        # Phase 60 (60-02, REVIEW-01/REVIEW-02): the Move-files review workspace -- the SIBLING of rename
        # over the SAME pending RenameProposal source (proposed_path facet, D-06). Same degrade-safe helper
        # and phaze-rw14 real-total bundle; oob_counts stays False (Pitfall 5).
        move_pending = await get_pending_proposal_rows(session)
        context["move_proposals"] = move_pending.rows
        context["move_pending_total"] = move_pending.total_pending
        context["move_high_confidence_pending"] = move_pending.high_confidence_pending
    elif stage == "propose":
        context |= await build_propose_list_context(request, session)
    elif stage == "tagwrite":
        # Phase 60 (60-03, REVIEW-01/REVIEW-02): the Tag-write review workspace renders the computed tag
        # comparison for EXECUTED files without a COMPLETED TagWriteLog (Pitfall 3 -- an empty queue while
        # files await a move is CORRECT). get_tagwrite_review_rows is a read-only, SAVEPOINT-wrapped,
        # degrade-safe assembly that returns [] on any DB error, so no router try/except is needed;
        # oob_counts stays False (Pitfall 5).
        #
        # phaze-bto9: the scan is capped at a fixed number of candidate batches, so on a large
        # already-correctly-tagged backlog it returns a bounded PREFIX of the queue instead of
        # walking every applied file. ``partial`` carries that into the subcount, which would
        # otherwise print a number that silently understates the real backlog.
        tagwrite_page = await get_tagwrite_review_page(session)
        context["tagwrite_files"] = tagwrite_page.rows
        context["tagwrite_partial"] = tagwrite_page.partial
    elif stage == "dedupe":
        # Phase 60 (60-04, REVIEW-03/REVIEW-05): the Dedupe keeper-select workspace renders the scored
        # duplicate groups (each keeper == score_group's canonical_id). get_dedupe_groups is a read-only,
        # SAVEPOINT-wrapped, degrade-safe assembly over the existing dedup reads (NO new query path, NO
        # enqueue, NO backend change) that returns [] on any DB error, so no router try/except is needed;
        # oob_counts stays False (Pitfall 5) -- the live sub-count would ride the single chrome poll's OOB seeds.
        context["dedupe_groups"] = await get_dedupe_groups(session)
    elif stage == "cue":
        # Phase 60 (60-04, REVIEW-04): the Cue preview workspace renders eligible + gated cue cards. Each
        # eligible card's .cue preview is built IN MEMORY (generate_cue_content, no disk write). get_cue_review_cards
        # is a read-only, SAVEPOINT-wrapped, degrade-safe assembly over the existing cue reads (NO write_cue_file,
        # NO enqueue, NO backend change) that returns [] on any DB error, so no router try/except is needed;
        # oob_counts stays False (Pitfall 5).
        context["cue_cards"] = await get_cue_review_cards(session)
    elif stage == "apply":
        # phaze-vvmh: the Apply workspace needs ONE read -- the aggregate proposal counts, in a single
        # query (services/proposal_queries.get_proposal_stats). They drive the EXECUTE APPROVED
        # button's enabled/disabled branch, its confirm copy, and the counter row that re-hosts the
        # useful half of the deleted proposals/partials/stats_bar.html. No enqueue, no write, no new
        # query path; oob_counts stays False (Pitfall 5) like every other review stage.
        context["stats"] = await get_proposal_stats(session)

    if wants_fragment(request):
        # phaze-a6hm.2 / .9: a live htmx swap has TWO shapes on this route, distinguished by what the
        # control aimed at. A rail click targets #stage-workspace and wants the whole workspace; a filter
        # tab, search keystroke or pager click targets the list container INSIDE that workspace and wants
        # only the list. Re-rendering the whole workspace for the latter would re-emit the search input
        # mid-keystroke (destroying focus and the caret) and duplicate the _workspace_poll_seeds OOB
        # targets, so the narrow swap is not an optimisation -- it is the correct answer.
        #
        # Discriminating on HX-Target (not HX-Request) is the established in-tree pattern for exactly
        # this "same URL, two swap shapes" case -- see _v7_row_target in routers/proposals.py, which
        # picks the v7 diff-row partial the same way. It is ALSO not a response_shape rule-1 violation:
        # wants_fragment has already made the fragment-vs-document decision above, and HX-Target only
        # refines WHICH fragment. The raw header this contract bans is HX-Request, and it is not read
        # here or anywhere else in this module.
        target = request.headers.get("HX-Target", "")
        if stage == "propose" and target == PROPOSE_LIST_CONTAINER_ID:
            return templates.TemplateResponse(request=request, name="pipeline/partials/_propose_list.html", context=context)
        return templates.TemplateResponse(request=request, name="shell/_stage_fragment.html", context=context)
    # A direct navigation, a bookmark, OR A HISTORY RESTORE lands here and gets the full shell. That
    # third case is phaze-a6hm.2's acceptance criterion and it needs NO extra code: because the filter
    # tabs, search box and pager all push /s/propose?... URLs (never a bare fragment endpoint), a restore
    # of a filtered URL re-enters THIS function, re-parses the same query string into the same
    # ListViewState above, and re-renders the same slice inside full chrome. The alternative design --
    # pushing a dedicated fragment endpoint's URL -- would have made every restore a fragment served into
    # <body>, i.e. the exact phaze-64uy defect response_shape.py rule 2 exists to prevent.
    return templates.TemplateResponse(request=request, name="shell/shell.html", context=context)


@router.get("/", response_class=HTMLResponse)
async def shell_home(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """GET / -- the shell root renders the Summary landing placeholder (SHELL-01, D-02 bare root).

    Quick 260707-sq3 (SQ3-02) repointed the default landing stage from Analyze to the static,
    DB-free Summary placeholder. Analyze is unchanged and stays one rail click away at
    ``/s/analyze``.
    """
    return await _render_stage(request, "summary", session)


@router.get("/s/{stage}", response_class=HTMLResponse)
async def shell_stage(request: Request, stage: str, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """GET /s/{stage} -- a single rail-node workspace.

    ``stage`` is whitelisted against ``STAGE_PARTIALS`` (DAG rail stages) OR ``UTILITY_PANES``
    (phaze-uvmcr.1's below-the-line utility panes) (D-02 per-stage validation owned here); an
    unknown stage (in neither map) 404s and is NEVER used to build a template path (T-57-01).
    """
    if stage not in STAGE_PARTIALS and stage not in UTILITY_PANES:
        raise HTTPException(status_code=404)
    return await _render_stage(request, stage, session)
