"""Static stage whitelists, the shell's Jinja environment, and the shared container ids.

Split out of the single-module ``routers/shell.py`` by phaze-bk9el.16. This is the leaf of the
package: it imports nothing from its siblings, so ``summary`` / ``stage_context`` / ``__init__``
can all depend on it without a cycle. Every value here is verbatim from the pre-split module --
the one line that had to change to move is ``TEMPLATES_DIR``, which gained a ``.parent`` because
this file sits one directory deeper than ``routers/shell.py`` did (the same three-hop walk
``routers/pipeline/_common.py`` documents, and the pitfall
``test_pipeline_package_facade.test_templates_dir_resolves_to_the_real_template_root`` exists for:
``Jinja2Templates`` accepts a non-existent directory happily and every render then fails at REQUEST
time, far from the cause).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from phaze.utils.humanize import relative_time
from phaze.web.static import static_asset_url
from phaze.web.template_globals import register_set_glyph_globals


# phaze-bk9el.16: THREE hops, not two. This module lives at routers/shell/stage_maps.py, one
# directory deeper than the pre-split routers/shell.py, so the walk up to src/phaze/templates
# needs parent.parent.parent. Mirrors routers/pipeline/_common.py, split the same way.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
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
register_set_glyph_globals(templates.env)

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


# Operations panes are deliberately separate from DAG stages: they have no pipeline count/badge or
# stage-active styling. T-57-01 requires static template literals, which also serve as dead-template
# entry roots. Audit and Agents reuse their redirect routes' context builders so the two render paths
# cannot drift (phaze-uvmcr.3/phaze-uvmcr.4).
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
# phaze-a6hm.2/.9: this ID is emitted by one host and narrow swaps return only its inner content,
# preventing the wrapper-nesting duplicate-ID class. Only the Propose stage owns it, and no OOB
# fragment targets it. Changes Review remains the sole authorization surface under
# ``docs/design/0008-changes-review-approval-boundary.md``.
PROPOSE_LIST_CONTAINER_ID = "propose-workspace-list"
CHANGES_LIST_CONTAINER_ID = "changes-workspace-list"
