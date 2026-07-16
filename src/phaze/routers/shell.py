"""v7.0 shell router -- owns ``GET /`` (Summary default) and ``GET /s/{stage}``.

This is the load-bearing spine of the v7.0 three-column "Hybrid Console" shell
(Phase 57). It serves the structural shell (header · DAG rail · ``#stage-workspace`` ·
right pane) on a direct/bookmark navigation, and a bare content fragment on an HTMX
rail swap -- the same HX-Request-aware full-page-vs-partial fork used by
``admin_agents.page`` (``routers/admin_agents.py``).

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
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.services.pipeline import (
    analyze_lanes_content_hash,
    get_files_page,
    get_fingerprint_pending_files,
    get_match_pending_tracklists,
    get_metadata_pending_files,
    get_scrape_pending_tracklists,
    get_stage_progress,
    get_trackid_stage_files,
    get_tracklist_set_rows,
    get_untracked_files,
)
from phaze.services.review import (
    get_cue_review_cards,
    get_dedupe_groups,
    get_pending_proposal_rows,
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


async def _analyze_file_count(session: AsyncSession) -> int:
    """Return the total ``FileRecord`` count, degrade-safe (RECORD-04).

    A lightweight ``COUNT(*)`` read. On ANY error it returns a non-zero sentinel so a
    transient DB issue can NEVER falsely trip the first-run empty state (better to show
    the normal dashboard than to wrongly claim the archive is empty).
    """
    try:
        result = await session.execute(select(func.count(FileRecord.id)))
    except Exception:
        # Roll back the aborted transaction so downstream reads on this same session
        # aren't poisoned (WR-05, matches the codebase-wide degrade-safe pattern).
        await session.rollback()
        return 1
    return int(result.scalar() or 0)


async def _render_stage(request: Request, stage: str, session: AsyncSession) -> HTMLResponse:
    """Render ``stage`` as the full shell (direct nav) or a bare fragment (HX rail swap).

    The fork is the same HX-Request-aware pattern as ``admin_agents.page``: an
    ``HX-Request: true`` swap gets the content-only ``shell/_stage_fragment.html``
    (which NEVER extends ``base.html`` -- a fragment carrying ``<html>``/``<head>``
    corrupts the shell, a ROADMAP-locked anti-pattern); a direct or bookmark
    navigation gets the full ``shell/shell.html``
    chrome. ``oob_counts=False`` so the initial render never emits the ``hx-swap-oob``
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
        # /pipeline/files links). The three keys mirror the route verbatim. stage/stage_partial/
        # oob_counts are re-asserted AFTER (defensive; the merge above only added base keys) so the
        # files context can never shadow the shell fork discriminators.
        context["files_page"] = await get_files_page(session, page=1, page_size=25, stage=None, bucket=None)
        context["active_stage"] = None
        context["active_bucket"] = None
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
    elif stage == "metadata":
        # Phase 58 (58-03, WORK-02): the Metadata workspace renders the metadata-pending queue --
        # the EXACT set its EXTRACT ALL button enqueues (get_metadata_pending_files: every
        # music/video FileRecord, D-01). Pitfall 5 -- the metadata stage had NO DB context before
        # this plan (only analyze/discover did). Reuses the existing shared pending-set helper (no
        # new service fn, no enqueue change). oob_counts stays False; the live sub-count refreshes
        # via the single chrome poll's OOB seeds.
        context["metadata_files"] = await get_metadata_pending_files(session)
    elif stage == "fingerprint":
        # Phase 58 (58-03, WORK-02): the Fingerprint workspace renders the fingerprint-pending
        # queue -- the EXACT set its FINGERPRINT ALL button enqueues (get_fingerprint_pending_files:
        # METADATA_EXTRACTED plus failed-retry, deduped, D-01). Pitfall 5 (no prior context for this
        # stage). Existing read only; no new service fn, no enqueue change.
        context["fingerprint_files"] = await get_fingerprint_pending_files(session)
    elif stage == "trackid":
        # Phase 59 (59-02, IDENT-01): the Track-ID workspace renders the combined per-file identity
        # table -- per-engine audfprint/Panako fingerprint state + tracklist match-state/confidence
        # (get_trackid_stage_files: a read-only, degrade-safe assembly over the existing
        # fingerprint_results + tracklists reads -- NO new query path, NO enqueue, NO backend change).
        # The helper returns [] on any DB error, so no router try/except is needed; oob_counts stays
        # False (Pitfall 5) -- the live sub-count refreshes via the single chrome poll's OOB seeds.
        context["trackid_files"] = await get_trackid_stage_files(session)
    elif stage == "tracklist":
        # Phase 59 (59-03, IDENT-02): the Tracklist workspace renders three Search/Scrape/Match step
        # cards (server-rendered done/total + pending counts) over the per-step ALL triggers, plus the
        # per-set N/M track-coverage table. get_stage_progress + get_tracklist_set_rows + the three
        # pending-set helpers are read-only, degrade-safe assemblies over the existing tracklist reads
        # (NO new query path, NO enqueue, NO backend change). The busy pills bind to the existing
        # searchBusy/scrapeBusy/matchBusy store keys (Pitfall 3 -- no new key, no second poll), so
        # oob_counts stays False (Pitfall 5); the live values ride the single chrome poll's OOB seeds.
        context["tracklist_steps"] = await get_stage_progress(session)
        context["tracklist_search_pending"] = len(await get_untracked_files(session))
        context["tracklist_scrape_pending"] = len(await get_scrape_pending_tracklists(session))
        context["tracklist_match_pending"] = len(await get_match_pending_tracklists(session))
        context["tracklist_sets"] = await get_tracklist_set_rows(session)
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
        # Phase 60 (60-03, D-01): the Propose generation view reuses the SAME degrade-safe pending-proposal
        # read as Rename/Move (it is a generation view over the shared RenameProposal source, NOT a diff).
        # The Model column renders the CONFIGURED settings.llm_model (A1 -- one model per run, not a per-row
        # value); it is a plain str read off the module-level ControlSettings singleton (no DB, no enqueue).
        # oob_counts stays False (Pitfall 5); the live sub-count would ride the single chrome poll's OOB seeds.
        context["propose_proposals"] = await get_pending_proposal_rows(session)
        context["llm_model"] = settings.llm_model
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

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="shell/_stage_fragment.html", context=context)
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
