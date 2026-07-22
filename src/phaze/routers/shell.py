"""v7.0 shell router -- owns ``GET /`` (Summary default) and ``GET /s/{stage}``.

This is the load-bearing spine of the v7.0 three-column "Hybrid Console" shell
(Phase 57). It serves the structural shell (header · DAG rail · ``#stage-workspace`` ·
right pane) on a direct/bookmark navigation, and a bare content fragment on an HTMX
rail swap -- the fork decided by ``response_shape.wants_fragment`` (contract rule 1),
the same predicate ``admin_agents.page`` (``routers/admin_agents.py``) composes.

Stage resolution is a strict whitelist: ``STAGE_PARTIALS`` maps each rail-node id to the
content partial that bridges it (D-01). ``stage`` is NEVER interpolated into a template
path -- the partial name always comes from this static dict, closing the
template-path-injection surface (T-57-01 / ASVS V5). An unknown stage 404s (D-02).

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
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.routers.proposal_sort import PROPOSE_SORT
from phaze.routers.response_shape import wants_fragment
from phaze.routers.view_state import PAGE_SIZE_CHOICES, ListViewState
from phaze.services.pipeline import (
    analyze_lanes_content_hash,
    get_files_page,
    get_match_pending_tracklists,
    get_scrape_pending_tracklists,
    get_stage_progress,
    get_untracked_files,
)
from phaze.services.review import (
    get_cue_review_cards,
    get_dedupe_groups,
    get_pending_proposal_rows,
    get_proposal_workspace_page,
    get_tagwrite_review_rows,
)
from phaze.services.route_control import get_route_control

from .pipeline import build_dashboard_context


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
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
    "files": "pipeline/partials/files_table_view.html",
    # Phase 58 (58-02, WORK-01): the first real workspace -- a static literal (T-57-01: `stage`
    # is never spliced into a template path). Supersedes-in-place; legacy templates stay until CUT-02.
    "discover": "pipeline/partials/discover_workspace.html",
    # Phase 58 (58-03, WORK-02): the Metadata + Fingerprint enrich workspaces -- static literals
    # (T-57-01: `stage` is never spliced into a template path). Supersede-in-place; legacy templates
    # stay until CUT-02.
    "metadata": "pipeline/partials/metadata_workspace.html",
    "fingerprint": "pipeline/partials/fingerprint_workspace.html",
    # Phase 58 (58-04, WORK-03/04): the real Analyze workspace (3 lane cards + reused cloud cards +
    # per-file lane/window table) supersedes the bridged dag_canvas.html -- a static literal (T-57-01:
    # `stage` is never spliced into a template path). dag_canvas.html stays reachable via the legacy
    # dashboard.html until CUT-02 (Phase 62), so the dead-template guard stays green (supersede-in-place).
    "analyze": "pipeline/partials/analyze_workspace.html",
    # Phase 59 (59-02, IDENT-01): the real Track-ID workspace (one combined per-file identity table
    # surfacing existing audfprint + Panako fingerprint state + tracklist match/confidence) supersedes
    # the placeholder -- a STATIC string literal (T-57-01: `stage` is never spliced into a template
    # path). Supersede-in-place; the legacy template stays reachable until CUT-02 (Phase 62).
    "trackid": "pipeline/partials/trackid_workspace.html",
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
}


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
    return {
        "propose_view": view,
        "sort": sort_state,
        "propose_proposals": page.rows,
        "row_select_ids": select_ids,
        "row_select_locked": select_locked,
        "select_name": "proposal_ids",
        "propose_pagination": page.pagination,
        "propose_stats": page.stats,
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
        "stage_partial": STAGE_PARTIALS[stage],
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
        context["stage_partial"] = STAGE_PARTIALS[stage]
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
        # 87-09 gap-fix: mounted as a WORKSPACE, so host the shared OOB seed-target placeholders (like
        # every other workspace via _workspace_scaffold) — else the single chrome /pipeline/stats poll's
        # OOB seeds (rail orphan badge, priority store, agent-busy gating) land nowhere on /s/files and log
        # htmx:oobErrorNoTarget every 5s. The pipeline_files() filter/pagination endpoint omits this flag,
        # so the fragment it swaps into #files-table-view never re-emits (and never duplicates) the seeds.
        context["include_poll_seeds"] = True
        context["stage"] = stage
        context["stage_partial"] = STAGE_PARTIALS[stage]
        context["oob_counts"] = False
    elif stage == "discover":
        # Phase 58 (58-02, WORK-01): the Discover workspace reuses the EXISTING recent-scans
        # data verbatim (build_recent_scans -- the SAME helper build_dashboard_context uses) and
        # the non-revoked agent list driving the reused Trigger Scan form. Both reads degrade-safe
        # at the service/ORM layer (no router try/except). oob_counts stays False on the stage
        # render (Pitfall 3); the live sub-count refreshes via the single chrome poll's OOB seeds.
        context["recent_scans"] = await build_recent_scans(session)
        # SER-01: only kind="fileserver" agents host media and can be scan targets;
        # exclude kind="compute" (media-less burst backends) from the picker.
        agents_stmt = select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)
        context["agents"] = (await session.execute(agents_stmt)).scalars().all()
    # phaze-5462: the metadata and fingerprint stages deliberately get NO file-list context here any
    # more. They used to seed `metadata_files` / `fingerprint_files` from get_*_pending_files, which
    # are UNBOUNDED (no LIMIT, no ORDER BY) -- the same latent cliff that made the Analyze tab ship
    # 12.7 MB. Those two tabs measured a harmless ~70 KB only because their backlogs happen to be
    # empty in production today, NOT because they were paged. Both workspaces now hx-get the bounded
    # GET /pipeline/pending-files fragment on load instead, so there is no file read on this path.
    # phaze-1wvb: the trackid stage deliberately gets NO file-list context here any more. It used to
    # seed `trackid_files` from get_trackid_stage_files, which was UNBOUNDED -- every music/video file
    # carrying any fingerprint row or a linked tracklist, `.all()`-materialised and server-rendered
    # into one table. As the archive converges that predicate approaches the WHOLE corpus, i.e. the
    # exact cliff phaze-5462 fixed on the Analyze tab. The workspace now hx-gets the bounded
    # GET /pipeline/trackid-files fragment on load, so there is no file read on this path at all.
    elif stage == "tracklist":
        # Phase 59 (59-03, IDENT-02): the Tracklist workspace renders three Search/Scrape/Match step
        # cards (server-rendered done/total + pending counts) over the per-step ALL triggers, plus the
        # per-set N/M track-coverage table (the latter now hx-get, below). get_stage_progress + the three
        # pending-set helpers are read-only, degrade-safe assemblies over the existing tracklist reads
        # (NO new query path, NO enqueue, NO backend change). The busy pills bind to the existing
        # searchBusy/scrapeBusy/matchBusy store keys (Pitfall 3 -- no new key, no second poll), so
        # oob_counts stays False (Pitfall 5); the live values ride the single chrome poll's OOB seeds.
        context["tracklist_steps"] = await get_stage_progress(session)
        context["tracklist_search_pending"] = len(await get_untracked_files(session))
        context["tracklist_scrape_pending"] = len(await get_scrape_pending_tracklists(session))
        context["tracklist_match_pending"] = len(await get_match_pending_tracklists(session))
        # phaze-1wvb: the per-set table is NOT seeded here any more -- get_tracklist_set_rows was an
        # unbounded row-per-Tracklist read rendered inline. The workspace hx-gets the bounded
        # GET /pipeline/tracklist-sets fragment on load instead. NOTE the three *_pending counts above
        # stay as they are: they feed the SEARCH/SCRAPE/MATCH ALL *enqueue* sets (paging contract
        # rule 7), which must never be paged.
    elif stage == "rename":
        # Phase 60 (60-02, REVIEW-01/REVIEW-02): the Rename/Path review workspace renders the pending
        # RenameProposal rows (filename facet) through the shared _diff_row.html. get_pending_proposal_rows
        # is a read-only, SAVEPOINT-wrapped, degrade-safe assembly over the existing proposal reads (NO
        # new query path, NO enqueue, NO backend change) that returns [] on any DB error, so no router
        # try/except is needed; oob_counts stays False (Pitfall 5) -- the live sub-count would ride the
        # single chrome poll's OOB seeds.
        context["rename_proposals"] = await get_pending_proposal_rows(session)
    elif stage == "move":
        # Phase 60 (60-02, REVIEW-01/REVIEW-02): the Move-files review workspace -- the SIBLING of rename
        # over the SAME pending RenameProposal source (proposed_path facet, D-06). Same degrade-safe helper;
        # oob_counts stays False (Pitfall 5).
        context["move_proposals"] = await get_pending_proposal_rows(session)
    elif stage == "propose":
        context |= await build_propose_list_context(request, session)
    elif stage == "tagwrite":
        # Phase 60 (60-03, REVIEW-01/REVIEW-02): the Tag-write review workspace renders the computed tag
        # comparison for EXECUTED files without a COMPLETED TagWriteLog (Pitfall 3 -- an empty queue while
        # files await a move is CORRECT). get_tagwrite_review_rows is a read-only, SAVEPOINT-wrapped,
        # degrade-safe assembly that returns [] on any DB error, so no router try/except is needed;
        # oob_counts stays False (Pitfall 5).
        context["tagwrite_files"] = await get_tagwrite_review_rows(session)
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

    ``stage`` is whitelisted against ``STAGE_PARTIALS`` (D-02 per-stage validation owned
    here); an unknown stage 404s and is NEVER used to build a template path (T-57-01).
    """
    if stage not in STAGE_PARTIALS:
        raise HTTPException(status_code=404)
    return await _render_stage(request, stage, session)
