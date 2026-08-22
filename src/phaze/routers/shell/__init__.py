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

PACKAGE LAYOUT (phaze-bk9el.16). This was one 1,392-line module -- the busiest file in the repo.
It is now a package whose ``__init__`` keeps BOTH routes and the render fork, and whose three
siblings hold the code those routes call:

* :mod:`~phaze.routers.shell.stage_maps` -- the static ``STAGE_PARTIALS`` / ``UTILITY_PANES`` /
  ``DOCUMENT_TITLES`` whitelists (T-57-01), the shell's Jinja environment, and the container ids.
  A leaf: it imports from no sibling.
* :mod:`~phaze.routers.shell.summary` -- the Summary overview derivations and their context builder.
* :mod:`~phaze.routers.shell.stage_context` -- every per-stage context builder and the
  ``_STAGE_CONTEXT_BUILDERS`` dispatch map.

``shell_home`` and ``shell_stage`` stay HERE, in ``phaze.routers.shell`` itself, deliberately:
``test_shell_characterization.test_shell_router_owns_exactly_the_recorded_routes`` identifies the
shell's routes by their endpoint's ``__module__`` and re-homing them to a sibling would silently
empty that check. The split moved code; it changed none of it. Every name the pre-split module
exposed is re-exported below, so ``from phaze.routers.shell import X`` is unchanged for importers
-- but a test that PATCHES one of those names must name the module that now owns it, because a
function resolves its globals from where it is defined, not from where it is re-exported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from phaze.database import get_session
from phaze.routers.response_shape import DUAL_SHAPE_RESPONSE_HEADERS, wants_fragment
from phaze.routers.shell.stage_context import (
    _STAGE_CONTEXT_BUILDERS,
    _agents_stage_context,
    _analyze_file_count,
    _analyze_stage_context,
    _apply_stage_context,
    _audit_stage_context,
    _cue_stage_context,
    _dedupe_stage_context,
    _discover_stage_context,
    _files_stage_context,
    _metadata_stage_context,
    _parse_selected_ids,
    _tracklist_stage_context,
    build_changes_review_context,
    build_propose_list_context,
)
from phaze.routers.shell.stage_maps import (
    _EMPTY_STATE_PARTIAL,
    CHANGES_LIST_CONTAINER_ID,
    DOCUMENT_TITLES,
    PROPOSE_LIST_CONTAINER_ID,
    STAGE_PARTIALS,
    TEMPLATES_DIR,
    UTILITY_PANES,
    _stage_partial,
    templates,
)
from phaze.routers.shell.summary import (
    SummaryOverviewInputs,
    _build_summary_context,
    _derive_recommended_action,
    _derive_summary_overview,
    _get_summary_aggregates,
    _summary_stage_status,
)
from phaze.services.route_control import get_route_control


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Re-exported so `from phaze.routers.shell import X` keeps working for every pre-split importer.
# NOTE (the phaze-oau1o lesson, from the routers/pipeline package split): no name here may collide
# with a SUBMODULE name -- a `from .summary import summary` style re-export would rebind the
# package attribute from the module to the object, and a patch target naming the submodule would
# then misresolve. `test_shell_package_facade` asserts that.
__all__ = [
    "CHANGES_LIST_CONTAINER_ID",
    "DOCUMENT_TITLES",
    "PROPOSE_LIST_CONTAINER_ID",
    "STAGE_PARTIALS",
    "TEMPLATES_DIR",
    "UTILITY_PANES",
    "SummaryOverviewInputs",
    "build_changes_review_context",
    "build_propose_list_context",
    "router",
    "templates",
]


router = APIRouter(tags=["shell"])


def _render_stage_fragment(request: Request, stage: str, context: dict[str, Any]) -> HTMLResponse:
    """Pick which of the THREE fragment bodies a live htmx swap gets.

    phaze-a6hm.2 / .9: a live htmx swap has TWO shapes on this route, distinguished by what the
    control aimed at. A rail click targets #stage-workspace and wants the whole workspace; a filter
    tab, search keystroke or pager click targets the list container INSIDE that workspace and wants
    only the list. Re-rendering the whole workspace for the latter would re-emit the search input
    mid-keystroke (destroying focus and the caret) and duplicate the _workspace_poll_seeds OOB
    targets, so the narrow swap is not an optimisation -- it is the correct answer.

    Discriminating on HX-Target (not HX-Request) is the established in-tree pattern for exactly
    this "same URL, two swap shapes" case -- see _v7_row_target in routers/proposals.py, which
    picks the v7 diff-row partial the same way. It is ALSO not a response_shape rule-1 violation:
    the caller's wants_fragment check has already made the fragment-vs-document decision, and
    HX-Target only refines WHICH fragment. The raw header this contract bans is HX-Request, and
    it is not read here or anywhere else in this module.

    phaze-r6e5m (response_shape.py contract rule 6): this URL legitimately serves THREE fragment
    bodies, so every branch carries DUAL_SHAPE_RESPONSE_HEADERS to keep the browser's HTTP cache
    from ever substituting one shape's cached bytes for another on a Back/Forward navigation.
    """
    target = request.headers.get("HX-Target", "")
    if stage == "files" and target == "files-table-view":
        return templates.TemplateResponse(
            request=request, name="pipeline/partials/files_table_view.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS
        )
    if stage == "propose" and target == PROPOSE_LIST_CONTAINER_ID:
        return templates.TemplateResponse(
            request=request, name="pipeline/partials/_propose_list.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS
        )
    if stage in {"rename", "move", "tagwrite"} and target == CHANGES_LIST_CONTAINER_ID:
        return templates.TemplateResponse(
            request=request, name="pipeline/partials/_changes_list.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS
        )
    return templates.TemplateResponse(request=request, name="shell/_stage_fragment.html", context=context, headers=DUAL_SHAPE_RESPONSE_HEADERS)


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

    Per-stage DB-backed context is built by :data:`_STAGE_CONTEXT_BUILDERS` (a stage absent
    from that map, currently only "operations", keeps the base context as-is). The shell
    context keys (``stage`` / ``stage_partial`` / ``oob_counts``) that a builder's own update
    would otherwise shadow are re-asserted INSIDE that builder's return value (see
    :func:`_analyze_stage_context`, :func:`_files_stage_context`, :func:`_agents_stage_context`),
    never here, so the merge below is always a plain ``context.update``. Each builder receives
    ``stage`` explicitly (rather than closing over it) so it stays correct under aliasing.
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
    builder = _STAGE_CONTEXT_BUILDERS.get(stage)
    if builder is not None:
        context.update(await builder(request, session, stage))

    if wants_fragment(request):
        return _render_stage_fragment(request, stage, context)
    # A direct navigation, a bookmark, OR A HISTORY RESTORE lands here and gets the full shell. That
    # third case is phaze-a6hm.2's acceptance criterion and it needs NO extra code: because the filter
    # tabs, search box and pager all push /s/propose?... URLs (never a bare fragment endpoint), a restore
    # of a filtered URL re-enters THIS function, re-parses the same query string into the same
    # ListViewState above, and re-renders the same slice inside full chrome. The alternative design --
    # pushing a dedicated fragment endpoint's URL -- would have made every restore a fragment served into
    # <body>, i.e. the exact phaze-64uy defect response_shape.py rule 2 exists to prevent.
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
