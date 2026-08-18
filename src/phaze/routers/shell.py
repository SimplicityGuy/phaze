"""v7.0 shell router -- owns ``GET /`` (Summary default) and ``GET /s/{stage}``.

This is the load-bearing spine of the v7.0 three-column "Hybrid Console" shell
(Phase 57). It serves the structural shell (header · DAG rail · ``#stage-workspace`` ·
right pane) on a direct/bookmark navigation, and a bare content fragment on an HTMX
rail swap -- the fork decided by ``response_shape.wants_fragment`` (contract rule 1),
the same predicate ``admin_agents.page`` (``routers/admin_agents.py``) composes.

Stage resolution is a strict whitelist across TWO static maps: ``STAGE_PARTIALS`` for the DAG
pipeline rail nodes (D-01), and its sibling ``UTILITY_PANES`` for Operations, Audit Log, and
Agents & Compute Lanes -- workspaces that are NOT DAG stages and so
are kept out of ``STAGE_PARTIALS``, whose own docstring pins its key set AND order verbatim to
the 57-UI-SPEC "DAG Rail" table. ``stage`` is NEVER interpolated into a template path -- the
partial name always comes from one of these two static dicts, closing the
template-path-injection surface (T-57-01 / ASVS V5). An unknown stage (in neither map) 404s
(D-02).

``GET /`` renders the actionable Summary overview. Analyze is one rail click away at
``/s/analyze``, where it still embeds the existing pipeline-dashboard content; its context is
built by the shared ``build_dashboard_context`` factored out of
``pipeline.dashboard()`` so the two paths cannot drift (D-01 / RESEARCH Open-Q2). The
remaining nodes render a minimal placeholder in Phase 57 -- their rich workspaces (and live
content bridges) land with their workspaces in Phases 58-61.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from phaze.config import settings
from phaze.database import get_session
from phaze.enums.stage import Stage, Status
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.routers.admin_agents import build_agents_pane_context
from phaze.routers.execution import build_audit_log_context
from phaze.routers.pipeline import FILES_SORT
from phaze.routers.pipeline_scans import RECENT_SCANS_SORT, build_recent_scans
from phaze.routers.proposal_sort import PROPOSE_SORT
from phaze.routers.response_shape import DUAL_SHAPE_RESPONSE_HEADERS, wants_fragment
from phaze.routers.view_state import PAGE_SIZE_CHOICES, ListViewState
from phaze.services.backends import derive_cloud_hold_reason, get_analysis_activity_counts, get_analysis_live_count
from phaze.services.dedup import count_duplicate_groups
from phaze.services.execution_preflight import get_execution_preflight
from phaze.services.pagination import DEFAULT_PAGE_SIZE, clamp_page, clamp_page_size
from phaze.services.pipeline import (
    ORPHANED_BUCKET,
    _read_in_own_session,
    _stats_fanout,
    analyze_lanes_content_hash,
    count_proposal_pending_files,
    get_analysis_stalled_count,
    get_awaiting_cloud_count,
    get_cached_stage_orphan_counts,
    get_cloud_phase_counts,
    get_files_page,
    get_inadmissible_count,
    get_match_pending_tracklists,
    get_metadata_activity_summary,
    get_metadata_selection_summary,
    get_metadata_status_snapshot,
    get_stage_activity_snapshot,
    get_stage_controls,
    get_stage_progress,
)
from phaze.services.proposal_queries import ProposalStats, get_proposal_stats
from phaze.services.review import (
    ChangesReviewStats,
    count_cue_review_candidates,
    get_changes_review_page,
    get_cue_review_cards,
    get_dedupe_groups,
    get_proposal_workspace_page,
    get_tagwrite_review_page,
)
from phaze.services.route_control import get_route_control
from phaze.services.stage_status import done_clause
from phaze.utils.humanize import relative_time
from phaze.web.static import static_asset_url

from .pipeline import build_dashboard_context


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# phaze-315t: fingerprinted, cache-forever static asset URLs. `shell/shell.html` is the
# top-level layout that carries the app.css link + favicon set.
templates.env.globals["static_url"] = static_asset_url
# phaze-uvmcr.4: admin/agents.html (UTILITY_PANES["agents"]) transitively includes
# admin/partials/agents_table.html, which calls {{ humanize_relative_time(...) }} for each agent's
# last-seen column -- the SAME global admin_agents.py's own (separate) Jinja2Templates instance
# registers for its unchanged fragment endpoints (_table/_activity/compute-lanes). Two Jinja
# environments, so the global must be registered on both; templates rendered through THIS env
# (shell.html, every STAGE_PARTIALS/UTILITY_PANES partial) resolve it from here.
templates.env.globals["humanize_relative_time"] = relative_time
router = APIRouter(tags=["shell"])

# Rail-node id -> bridged content partial (D-01). The keys + their order are VERBATIM
# from the prototype RAIL config (57-UI-SPEC "DAG Rail" table); every node now resolves
# to its redesigned per-stage workspace (Phases 58-61). Every VALUE is a STATIC string
# literal: `stage` is matched against these keys and never spliced into a template path
# (T-57-01 -- template-path-injection mitigation). The literals also act as the
# dead-template guard's entry roots, so each stays reachable.
STAGE_PARTIALS: dict[str, str] = {
    # The actionable collection/pipeline overview. FIRST key so the dict order matches the rail.
    # The static literal preserves the T-57-01 template-path-injection boundary.
    "summary": "shell/partials/summary_overview.html",
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
    "rename": "pipeline/partials/changes_workspace.html",
    # Phase 60 (60-03, REVIEW-01/REVIEW-02): the real Tag-write review workspace (the shared _diff_row.html
    # over the computed tag comparison -- APPROVE POSTs /tags/{id}/write, bulk POSTs the D-03 server-predicate
    # /tags/bulk-write-no-discrepancies) supersedes the placeholder -- a STATIC string literal (T-57-01).
    # Compatibility aliases render the canonical workspace. They are intentionally absent from the
    # rail, so there is one approval path without breaking old bookmarks.
    "tagwrite": "pipeline/partials/changes_workspace.html",
    "move": "pipeline/partials/changes_workspace.html",
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


# Rail-node id -> content partial for the Operations workspaces -- the sibling of
# STAGE_PARTIALS above, deliberately kept
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
# phaze-uvmcr.1 landed both keys with placeholder/thin content so that bead was independently
# mergeable.
#
# phaze-uvmcr.3: "audit" now points at the REAL content -- execution/audit_log.html, converted
# from a base.html-extending full page into a content-only partial. Its context is built by
# build_audit_log_context (routers/execution.py, imported above), the SAME function GET /audit/'s
# redirect target composes with -- shared so the shell-hosted pane and the (now-redirecting)
# legacy route can never diverge on what a render of this content needs.
#
# phaze-uvmcr.4: "agents" now points at the REAL content -- admin/agents.html, converted from a
# base.html-extending full page into a content-only partial (no more {% extends %}, no
# document-level tags). Its context is built by build_agents_pane_context (routers/admin_agents.py,
# imported above), the SAME function GET /admin/agents's redirect target composed with before this
# bead -- shared so the shell-hosted pane and the (now-redirecting) legacy route can never diverge
# on what a render of this content needs. See the ``elif stage == "agents"`` branch below.
UTILITY_PANES: dict[str, str] = {
    "operations": "shell/partials/operations.html",
    "audit": "execution/audit_log.html",
    "agents": "admin/agents.html",
}

DOCUMENT_TITLES: dict[str, str] = {
    "summary": "Summary",
    "files": "Files",
    "discover": "Discover",
    "metadata": "Metadata",
    "analyze": "Analyze",
    "tracklist": "Tracklists",
    "propose": "Propose changes",
    "rename": "Changes Review",
    "tagwrite": "Changes Review",
    "move": "Changes Review",
    "dedupe": "Duplicates",
    "cue": "Cue sheets",
    "apply": "Execute approved",
    "operations": "Routing operations",
    "audit": "Audit log",
    "agents": "Agents and compute lanes",
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
#    response (_propose_bulk_response.html) -- and it obeyed the same split, including
#    _propose_list.html (inner content) and never the wrapper. phaze-7tiqp retired that producer
#    with the rest of the Propose bulk chain: ADR-0008 made Changes Review the only surface that
#    authorizes anything, so Propose has no bulk controls and PATCH /proposals/bulk no longer has a
#    branch that renders into this container. Two producers again, both listed above.
# 5. No OOB fragment targets it: this container is only ever an hx-target, and oob_counts stays False
#    on every stage render (Pitfall 5), so the chrome poll's OOB seeds cannot land here.
PROPOSE_LIST_CONTAINER_ID = "propose-workspace-list"
CHANGES_LIST_CONTAINER_ID = "changes-workspace-list"


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


def _summary_stage_status(stage: dict[str, int | None]) -> dict[str, str | bool]:
    """Map an authoritative stage-progress bucket to the shared status vocabulary."""
    total = int(stage.get("total") or 0)
    done = int(stage.get("done") or 0)
    skipped = int(stage.get("skipped") or 0)
    failed = int(stage.get("failed") or 0)
    in_flight = int(stage.get("in_flight") or 0)
    if failed:
        return {"label": "failed", "tone": "danger", "icon": "✕", "pulse": False}
    if in_flight:
        return {"label": "in flight", "tone": "accent", "icon": "●", "pulse": True}
    if total and done + skipped >= total:
        return {"label": "complete", "tone": "success", "icon": "✓", "pulse": False}
    if total:
        return {"label": "not started", "tone": "neutral", "icon": "—", "pulse": False}
    return {"label": "empty", "tone": "neutral", "icon": "—", "pulse": False}


def _derive_summary_overview(
    stage_progress: dict[str, dict[str, int | None]],
    *,
    proposal_pending: int,
    proposal_approved: int,
    active_fileservers: int | None,
    orphan_counts: dict[str, int],
    stalled_analyses: int,
    inadmissible_count: int,
    awaiting_cloud_count: int,
    awaiting_hold_reason: str | None,
    queued_behind_quota_count: int,
    stage_paused: dict[str, bool],
    enriched_count: int | None,
    analysis_live: int | None,
    analyses_today: int | None,
    analyses_lifetime: int | None,
) -> dict[str, Any]:
    """Derive display-only overview state without introducing new pipeline semantics."""
    discovery = stage_progress["discovery"]
    metadata = stage_progress["metadata"]
    analyze = stage_progress["analyze"]
    proposals = stage_progress["proposals"]
    execute = stage_progress["execute"]
    total_files = int(discovery["done"] or 0)

    flow = [
        {"name": "Discover", "href": "/s/discover", "done": total_files, "total": total_files, "status": _summary_stage_status(discovery)},
        {
            "name": "Metadata",
            "href": "/s/metadata",
            "done": int(metadata["done"] or 0),
            "total": int(metadata["total"] or 0),
            "status": _summary_stage_status(metadata),
        },
        {
            "name": "Analyze",
            "href": "/s/analyze",
            "done": int(analyze["done"] or 0),
            "total": int(analyze["total"] or 0),
            "status": _summary_stage_status(analyze),
        },
        {
            "name": "Propose",
            "href": "/s/propose",
            "done": int(proposals["done"] or 0),
            "total": int(proposals["total"] or 0),
            "status": _summary_stage_status(proposals),
        },
        {
            "name": "Review",
            "href": "/s/rename",
            "done": None,
            "total": proposal_pending,
            "detail": f"{proposal_pending} awaiting decision",
            "status": (
                {"label": "attention", "tone": "attention", "icon": "!", "pulse": False}
                if proposal_pending
                else {"label": "caught up", "tone": "success", "icon": "✓", "pulse": False}
            ),
        },
        {
            "name": "Apply",
            "href": "/s/apply",
            "done": int(execute["done"] or 0),
            "total": proposal_approved,
            "detail": f"{proposal_approved} approved now",
            "status": (
                {"label": "ready", "tone": "attention", "icon": "!", "pulse": False}
                if proposal_approved
                else {"label": "caught up", "tone": "success", "icon": "✓", "pulse": False}
            ),
        },
    ]

    attention: list[dict[str, str | int]] = []

    def add_attention(priority: int, title: str, detail: str, href: str, action: str, tone: str = "attention") -> None:
        attention.append({"priority": priority, "title": title, "detail": detail, "href": href, "action": action, "tone": tone})

    metadata_failed = int(metadata.get("failed") or 0)
    analyze_failed = int(analyze.get("failed") or 0)
    if metadata_failed:
        add_attention(10, "Metadata failures", f"{metadata_failed} file(s) need inspection or retry.", "/s/metadata", "Open Metadata", "danger")
    if analyze_failed:
        stalled_detail = f"; {stalled_analyses} stopped by the progress watchdog" if stalled_analyses else ""
        add_attention(
            11, "Analysis failures", f"{analyze_failed} file(s) reached terminal failure{stalled_detail}.", "/s/analyze", "Open Analyze", "danger"
        )
    orphan_total = int(orphan_counts.get("metadata", 0)) + int(orphan_counts.get("analyze", 0))
    if orphan_total:
        add_attention(
            20,
            "Orphaned work",
            f"{orphan_total} scheduled file(s) have no live job or domain result.",
            "/s/discover",
            "Open Recovery",
        )
    if inadmissible_count:
        add_attention(
            30, "Cloud jobs blocked by configuration", f"{inadmissible_count} active cloud job(s) are Inadmissible.", "/s/agents", "Inspect Compute"
        )
    if awaiting_cloud_count:
        hold_reason = awaiting_hold_reason or "reason unavailable"
        if hold_reason in {"cloud routing disabled", "held — cloud routing paused (force-local)"}:
            awaiting_href, awaiting_action = "/s/operations", "Open Routing"
        elif hold_reason in {
            "held — no cloud backend reachable",
            "held — no fileserver agent online",
        } or hold_reason.startswith("held — all lanes at capacity"):
            awaiting_href, awaiting_action = "/s/agents", "Inspect Compute"
        else:
            awaiting_href, awaiting_action = "/s/analyze", "Open Analyze"
        add_attention(
            40,
            "Files awaiting cloud routing",
            f"{awaiting_cloud_count} file(s): {hold_reason}.",
            awaiting_href,
            awaiting_action,
        )
    if queued_behind_quota_count:
        add_attention(
            41,
            "Cloud quota wait",
            f"{queued_behind_quota_count} submitted cloud job(s) are waiting for cluster quota.",
            "/s/agents",
            "Inspect Compute",
        )
    for priority, stage_name, stage_label, stage_bucket in (
        (45, "metadata", "Metadata enrichment", metadata),
        (46, "analyze", "Audio analysis", analyze),
    ):
        if stage_paused[stage_name] and (int(stage_bucket.get("not_started") or 0) or int(stage_bucket.get("in_flight") or 0)):
            add_attention(
                priority,
                f"{stage_label} is paused",
                f"{int(stage_bucket.get('not_started') or 0)} not started; {int(stage_bucket.get('in_flight') or 0)} still marked in flight.",
                f"/s/{stage_name}",
                f"Open {stage_label}",
            )
    if active_fileservers == 0:
        add_attention(
            50,
            "No file-server agent available",
            "Discovery and file-owned work cannot be dispatched until an agent checks in.",
            "/s/agents",
            "Configure Agents",
        )
    attention.sort(key=lambda item: int(item["priority"]))

    if attention:
        first = attention[0]
        recommended = {"title": first["title"], "detail": first["detail"], "href": first["href"], "action": first["action"], "tone": first["tone"]}
    elif total_files == 0:
        recommended = {
            "title": "Discover the collection",
            "detail": "Run a scan from a configured file-server agent to populate the collection.",
            "href": "/s/discover",
            "action": "Open Discover",
            "tone": "accent",
        }
    elif int(metadata.get("not_started") or 0) and not stage_paused["metadata"] and not int(metadata.get("in_flight") or 0):
        recommended = {
            "title": "Start metadata enrichment",
            "detail": "Metadata and analysis are independent and can run in parallel.",
            "href": "/s/metadata",
            "action": "Open Metadata",
            "tone": "accent",
        }
    elif int(analyze.get("not_started") or 0) and not stage_paused["analyze"] and not int(analyze.get("in_flight") or 0):
        recommended = {
            "title": "Start audio analysis",
            "detail": "Analysis can run independently of metadata enrichment.",
            "href": "/s/analyze",
            "action": "Open Analyze",
            "tone": "accent",
        }
    elif stage_paused["metadata"] and (int(metadata.get("not_started") or 0) or int(metadata.get("in_flight") or 0)):
        recommended = {
            "title": "Metadata enrichment is paused",
            "detail": "Review the pause before resuming metadata work.",
            "href": "/s/metadata",
            "action": "Open Metadata",
            "tone": "attention",
        }
    elif stage_paused["analyze"] and (int(analyze.get("not_started") or 0) or int(analyze.get("in_flight") or 0)):
        recommended = {
            "title": "Audio analysis is paused",
            "detail": "Review the pause before resuming analysis work.",
            "href": "/s/analyze",
            "action": "Open Analyze",
            "tone": "attention",
        }
    elif int(metadata.get("in_flight") or 0) or int(analyze.get("in_flight") or 0):
        active_stage = "metadata" if int(metadata.get("in_flight") or 0) else "analyze"
        recommended = {
            "title": "Enrichment is in progress",
            "detail": "Current work is already running; monitor it before starting the next dependent stage.",
            "href": f"/s/{active_stage}",
            "action": f"Open {active_stage.title()}",
            "tone": "accent",
        }
    elif int(proposals["done"] or 0) < int(proposals["total"] or 0):
        recommended = {
            "title": "Generate proposals",
            "detail": "Files with completed metadata and analysis are ready for proposal generation.",
            "href": "/s/propose",
            "action": "Open Propose",
            "tone": "accent",
        }
    elif proposal_pending:
        recommended = {
            "title": "Review proposed changes",
            "detail": f"{proposal_pending} proposal(s) await an operator decision.",
            "href": "/s/rename",
            "action": "Open Review",
            "tone": "attention",
        }
    elif proposal_approved:
        recommended = {
            "title": "Execute approved changes",
            "detail": f"{proposal_approved} approved proposal(s) are ready to apply.",
            "href": "/s/apply",
            "action": "Open Apply",
            "tone": "attention",
        }
    else:
        recommended = {
            "title": "Pipeline caught up",
            "detail": "No failures, recovery candidates, pending reviews, or approved changes need action.",
            "href": "/s/files",
            "action": "Browse Files",
            "tone": "success",
        }

    return {
        "total_files": total_files,
        "resolved_enrichment": enriched_count,
        "proposal_pending": proposal_pending,
        "proposal_approved": proposal_approved,
        "flow": flow,
        "attention": attention,
        "recommended": recommended,
        "recent": {
            "live": analysis_live,
            "today": analyses_today,
            "lifetime": analyses_lifetime,
        },
        "is_empty": total_files == 0,
        "is_partially_configured": active_fileservers == 0,
        "is_degraded": bool(metadata_failed or analyze_failed or orphan_total or inadmissible_count or awaiting_cloud_count),
    }


async def _get_summary_aggregates(session: AsyncSession) -> dict[str, int | None]:
    """Read the enriched-file intersection and active file-server count in one statement."""
    enriched = select(func.count(FileRecord.id)).where(done_clause(Stage.METADATA), done_clause(Stage.ANALYZE)).scalar_subquery()
    active_fileservers = (
        select(func.count(Agent.id)).where(Agent.kind == "fileserver", Agent.revoked_at.is_(None), Agent.last_seen_at.is_not(None)).scalar_subquery()
    )
    try:
        async with session.begin_nested():
            row = (await session.execute(select(enriched, active_fileservers))).one()
    except Exception:
        return {"enriched": None, "active_fileservers": None}
    return {
        "enriched": int(row[0] or 0),
        "active_fileservers": int(row[1] or 0),
    }


async def _build_summary_context(app_state: Any, session: AsyncSession) -> dict[str, Any]:
    """Read independent Summary sources in own sessions under the shared stats fan-out cap."""
    fanout = _stats_fanout()
    (
        stage_progress,
        proposal_stats,
        stalled_analyses,
        inadmissible_count,
        awaiting_cloud_count,
        cloud_phases,
        stage_controls,
        aggregates,
        analysis_live,
        analysis_activity,
    ) = cast(
        "tuple[dict[str, dict[str, int | None]], ProposalStats, int, int, int, dict[str, int], dict[str, dict[str, int | bool]], dict[str, int | None], int | None, dict[str, int | None]]",
        await asyncio.gather(
            # get_stage_progress already owns its bounded per-node sessions; wrapping it would hold
            # a semaphore slot while its children wait for that same semaphore.
            get_stage_progress(session),
            _read_in_own_session(
                fanout,
                get_proposal_stats,
                ProposalStats(total=0, pending=0, approved=0, executed=0, rejected=0, failed=0, avg_confidence=None),
            ),
            _read_in_own_session(fanout, get_analysis_stalled_count, 0),
            _read_in_own_session(fanout, get_inadmissible_count, 0),
            _read_in_own_session(fanout, get_awaiting_cloud_count, 0),
            _read_in_own_session(
                fanout,
                get_cloud_phase_counts,
                {"queued_behind_quota": 0, "admitted": 0, "running": 0, "finished": 0},
            ),
            _read_in_own_session(
                fanout,
                get_stage_controls,
                {"metadata": {"paused": False, "priority": 50}, "analyze": {"paused": False, "priority": 50}},
            ),
            _read_in_own_session(
                fanout,
                _get_summary_aggregates,
                cast("dict[str, int | None]", {"enriched": None, "active_fileservers": None}),
            ),
            _read_in_own_session(fanout, lambda own_session: get_analysis_live_count(own_session, app_state), None),
            _read_in_own_session(
                fanout,
                get_analysis_activity_counts,
                cast("dict[str, int | None]", {"today": None, "lifetime": None}),
            ),
        ),
    )
    awaiting_hold_reason = await _read_in_own_session(fanout, derive_cloud_hold_reason, "held") if awaiting_cloud_count else None
    return {
        "summary": _derive_summary_overview(
            stage_progress,
            proposal_pending=proposal_stats.pending,
            proposal_approved=proposal_stats.approved,
            active_fileservers=aggregates["active_fileservers"],
            orphan_counts=get_cached_stage_orphan_counts(),
            stalled_analyses=stalled_analyses,
            inadmissible_count=inadmissible_count,
            awaiting_cloud_count=awaiting_cloud_count,
            awaiting_hold_reason=awaiting_hold_reason,
            queued_behind_quota_count=cloud_phases["queued_behind_quota"],
            stage_paused={stage: bool(stage_controls[stage]["paused"]) for stage in ("metadata", "analyze")},
            enriched_count=aggregates["enriched"],
            analysis_live=analysis_live,
            analyses_today=analysis_activity["today"],
            analyses_lifetime=analysis_activity["lifetime"],
        )
    }


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


async def build_changes_review_context(request: Request, session: AsyncSession) -> dict[str, Any]:
    """Build the canonical review list from URL-borne filter, page, and selection state."""
    view = ListViewState.from_request(request, status="needs_review", sort="confidence")
    allowed_statuses = {"all", "needs_review", "approved", "blocked", "rejected"}
    if view.status not in allowed_statuses:
        view = view.with_(status="needs_review")

    selected: list[str] = []
    for raw in request.query_params.get("selected", "").split(","):
        try:
            selected.append(str(uuid.UUID(raw)))
        except ValueError:
            continue
        if len(selected) >= 100:
            break

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
        "document_title": DOCUMENT_TITLES[stage],
        "oob_counts": False,
        # Phase 71 (71-04, BEUI-02): seed the header force-local pill's state on EVERY page from the
        # durable route_control 'global' row (get_route_control is degrade-safe -> False on any DB
        # error, never raises). Seeded HERE in the base shell context -- NOT the Analyze-only
        # build_dashboard_context -- so the global incident control shows correct state everywhere.
        "force_local": await get_route_control(session),
    }
    if stage == "summary":
        context.update(await _build_summary_context(request.app.state, session))
    elif stage == "analyze":
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
        context["files_page"] = await get_files_page(
            session,
            page=page,
            page_size=page_size,
            stage=active_stage,
            bucket=active_bucket,
            sort=files_sort_state,
        )
        context["active_stage"] = active_stage.value if active_stage is not None else None
        context["active_bucket"] = active_bucket
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
    elif stage == "metadata":
        context["metadata_activity"] = await get_metadata_activity_summary(session)
        context["metadata_selection"] = await get_metadata_selection_summary(session)
        context["metadata_status"] = await get_metadata_status_snapshot(session)
        context["metadata_queue"] = await get_stage_activity_snapshot(session)
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
    elif stage in {"rename", "move", "tagwrite"}:
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
        context |= await build_changes_review_context(request, session)
    elif stage == "propose":
        context |= await build_propose_list_context(request, session)
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
        # phaze-vvmh: the aggregate proposal counts, in a single query
        # (services/proposal_queries.get_proposal_stats). They drive the counter row that re-hosts the
        # useful half of the deleted proposals/partials/stats_bar.html. No enqueue, no write, no new
        # query path; oob_counts stays False (Pitfall 5) like every other review stage.
        context["stats"] = await get_proposal_stats(session)
        # phaze-tzy6s.12 / D-10: the preflight manifest. It no longer suffices to know how many
        # proposals are approved -- Execute is the one control that moves bytes, so the operator gets
        # the full manifest (which operations, against which agents, what conflicts, what is excluded
        # and why, what is reversible) BEFORE committing. get_execution_preflight deliberately reuses
        # start_execution's OWN reads so the manifest cannot drift from the dispatch it describes; see
        # that module's D-10 record before adding a read here.
        #
        # The three adjacent counts are work this control does NOT dispatch. They are passed in rather
        # than re-derived inside the service so the "does not participate" rows carry live numbers
        # instead of a confident zero -- an operation type silently absent from a manifest reads as
        # "there is none of it". All three helpers are degrade-safe (0 on any DB error), so a
        # failure here costs an exclusion row, never the render.
        #
        # phaze-tzy6s.17: these are COUNT queries. They used to be `len()` of the three list readers,
        # which was wrong twice over on the one screen that must not be wrong:
        #
        #   * The numbers saturated silently. get_dedupe_groups pages at limit=100, so 100 groups and
        #     100,000 groups both rendered "100"; get_cue_review_cards caps each half at
        #     _MAX_REVIEW_ROWS. Those figures reach "Will not run — N excluded" AND the confirmation
        #     dialog body, i.e. the last thing an operator reads before an irreversible batch.
        #   * They did enormous discarded work to produce three integers -- full duplicate detection
        #     with score_group/build_dupe_group_card per group, and in-memory .cue generation via
        #     generate_cue_content for up to _MAX_REVIEW_ROWS sets -- then kept only len().
        #
        # Tag writes get the other treatment because they cannot get this one: eligibility is a Python
        # predicate (compute_proposed_tags per candidate), so there is no COUNT to run. The scan stays,
        # and `partial` -- which nothing read before -- is now plumbed through so a saturated figure
        # renders as "N+" instead of posing as a total. A row cap hit with the candidate set not
        # provably exhausted counts as saturated too; TagwriteReviewPage.partial already means exactly
        # that (phaze-a2ytu), so it is the whole condition and len(rows) >= cap is not re-derived here.
        tagwrite_page = await get_tagwrite_review_page(session)
        context["preflight"] = await get_execution_preflight(
            session,
            pending=context["stats"].pending,
            rejected=context["stats"].rejected,
            tagwrite_pending=len(tagwrite_page.rows),
            tagwrite_pending_at_least=tagwrite_page.partial,
            dedupe_pending=await count_duplicate_groups(session),
            cue_pending=await count_cue_review_candidates(session),
        )
    elif stage == "audit":
        # phaze-uvmcr.3: the /s/audit utility-pane host (UTILITY_PANES, not STAGE_PARTIALS -- it
        # is not a DAG pipeline stage). ``execution.audit_log``'s non-fragment branch now redirects
        # a plain request / bookmark / history-restore of GET /audit/ HERE, carrying the request's
        # whole query string, so the status/page/page_size/sort/order the redirect arrives with
        # must resolve to the SAME filtered view on this side -- not silently reset to page 1 / all
        # statuses. Parsed straight off ``request.query_params`` (not threaded through
        # ``shell_stage``'s signature, which stays generic across every rail node, DAG or utility
        # alike) and clamped with the SAME ``phaze.services.pagination`` helpers every other paged
        # read in this router composes, so an absurd/unparseable value degrades to the same safe
        # default a hand-edited URL gets everywhere else (never a 422 on a render path).
        #
        # The context itself comes from :func:`build_audit_log_context`, the SAME function the
        # GET /audit/ fragment endpoint (the filter tab / pager / column-sort swap target, still at
        # its own path, unchanged) calls -- so the two producers of "what audit log content looks
        # like" cannot independently drift (phaze-a6hm.11's build_propose_list_context discipline,
        # applied here).
        audit_params = request.query_params
        audit_status = audit_params.get("status")
        try:
            audit_page_num = clamp_page(int(audit_params.get("page", "1")))
        except ValueError:
            audit_page_num = clamp_page(1)
        try:
            audit_page_size = clamp_page_size(int(audit_params.get("page_size", str(DEFAULT_PAGE_SIZE))))
        except ValueError:
            audit_page_size = clamp_page_size(DEFAULT_PAGE_SIZE)
        context |= await build_audit_log_context(
            session,
            status=audit_status,
            page=audit_page_num,
            page_size=audit_page_size,
            sort=audit_params.get("sort"),
            order=audit_params.get("order"),
        )
    elif stage == "agents":
        # phaze-uvmcr.4: the Compute/Agents UTILITY_PANES stage -- below-the-line, not a DAG stage
        # (hence living last here, mirroring the below-the-line placement in rail.html) but forked through
        # the exact same wants_fragment/history-restore machinery as every stage above (H1). Its
        # context is built by admin_agents.build_agents_pane_context, the SAME assembly GET
        # /admin/agents's redirect target used to build inline before this bead -- shared so this pane
        # and the (now-redirecting) legacy route can never diverge on what a render needs. That
        # function reads ?agent=/?clane=/?sort=/?order= straight off request.query_params (mirroring
        # the ``analyze`` branch's ?lane= resolution above) rather than declaring typed Query()
        # params on THIS route, so the wire-bounds contract stays scoped to the routes that actually
        # declare them (/admin/agents/_table, unchanged).
        context |= await build_agents_pane_context(request, session)
        context["stage"] = stage
        context["stage_partial"] = _stage_partial(stage)
        context["oob_counts"] = False

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
        if stage == "files" and target == "files-table-view":
            return templates.TemplateResponse(
                request=request,
                name="pipeline/partials/files_table_view.html",
                context=context,
                headers=DUAL_SHAPE_RESPONSE_HEADERS,
            )
        if stage == "propose" and target == PROPOSE_LIST_CONTAINER_ID:
            return templates.TemplateResponse(
                request=request, name="pipeline/partials/_propose_list.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS
            )
        if stage in {"rename", "move", "tagwrite"} and target == CHANGES_LIST_CONTAINER_ID:
            return templates.TemplateResponse(
                request=request,
                name="pipeline/partials/_changes_list.html",
                context=context,
                headers=DUAL_SHAPE_RESPONSE_HEADERS,
            )
        return templates.TemplateResponse(request=request, name="shell/_stage_fragment.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS)
    # A direct navigation, a bookmark, OR A HISTORY RESTORE lands here and gets the full shell. That
    # third case is phaze-a6hm.2's acceptance criterion and it needs NO extra code: because the filter
    # tabs, search box and pager all push /s/propose?... URLs (never a bare fragment endpoint), a restore
    # of a filtered URL re-enters THIS function, re-parses the same query string into the same
    # ListViewState above, and re-renders the same slice inside full chrome. The alternative design --
    # pushing a dedicated fragment endpoint's URL -- would have made every restore a fragment served into
    # <body>, i.e. the exact phaze-64uy defect response_shape.py rule 2 exists to prevent.
    #
    # phaze-r6e5m (response_shape.py contract rule 6): this URL legitimately serves THREE bodies
    # (the two fragment branches above plus this full document), so every branch -- this one
    # included -- carries DUAL_SHAPE_RESPONSE_HEADERS to keep the browser's HTTP cache from ever
    # substituting one shape's cached bytes for another on a Back/Forward navigation.
    return templates.TemplateResponse(request=request, name="shell/shell.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS)


@router.get("/", response_class=HTMLResponse)
async def shell_home(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """GET / -- render the actionable Summary overview as the shell default."""
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
