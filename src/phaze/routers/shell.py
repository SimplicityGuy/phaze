"""v7.0 shell router -- owns ``GET /`` (Analyze default) and ``GET /s/{stage}``.

This is the load-bearing spine of the v7.0 three-column "Hybrid Console" shell
(Phase 57). It serves the structural shell (header · DAG rail · ``#stage-workspace`` ·
right pane) on a direct/bookmark navigation, and a bare content fragment on an HTMX
rail swap -- the fragment-vs-full fork mirrored VERBATIM from ``search.py:73-77``.

Stage resolution is a strict whitelist: ``STAGE_PARTIALS`` maps each rail-node id to the
content partial that bridges it (D-01). ``stage`` is NEVER interpolated into a template
path -- the partial name always comes from this static dict, closing the
template-path-injection surface (T-57-01 / ASVS V5). An unknown stage 404s (D-02).

The Analyze node (the ``/`` default) embeds the existing pipeline-dashboard content
(``dag_canvas.html``); its context is built by the shared ``build_dashboard_context``
factored out of ``pipeline.dashboard()`` so the two paths cannot drift (D-01 / RESEARCH
Open-Q2). The remaining nodes render a minimal placeholder in Phase 57 -- their rich
workspaces (and live content bridges) land with their workspaces in Phases 58-61.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.routers.pipeline_scans import build_recent_scans
from phaze.services.pipeline import get_fingerprint_pending_files, get_metadata_pending_files

from .pipeline import build_dashboard_context


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["shell"])

# Rail-node id -> bridged content partial (D-01). The keys + their order are VERBATIM
# from the prototype RAIL config (57-UI-SPEC "DAG Rail" table); `analyze` (the `/`
# default) embeds the existing pipeline-dashboard DAG canvas, which Phase 58 replaces
# with lane cards. The other 11 nodes render a shared placeholder in Phase 57 -- their
# real content bridges arrive with their redesigned workspaces in Phases 58-61. Every
# VALUE is a STATIC string literal: `stage` is matched against these keys and never
# spliced into a template path (T-57-01 -- template-path-injection mitigation). The
# literals also act as the dead-template guard's entry roots, so each stays reachable.
_STAGE_PLACEHOLDER = "shell/partials/_stage_placeholder.html"
STAGE_PARTIALS: dict[str, str] = {
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
    "trackid": _STAGE_PLACEHOLDER,
    "tracklist": _STAGE_PLACEHOLDER,
    "propose": _STAGE_PLACEHOLDER,
    "rename": _STAGE_PLACEHOLDER,
    "tagwrite": _STAGE_PLACEHOLDER,
    "move": _STAGE_PLACEHOLDER,
    "dedupe": _STAGE_PLACEHOLDER,
    "cue": _STAGE_PLACEHOLDER,
}


async def _render_stage(request: Request, stage: str, session: AsyncSession) -> HTMLResponse:
    """Render ``stage`` as the full shell (direct nav) or a bare fragment (HX rail swap).

    The fork mirrors ``search.py:73-77`` VERBATIM: an ``HX-Request: true`` swap gets the
    content-only ``shell/_stage_fragment.html`` (which NEVER extends ``base.html`` -- a
    fragment carrying ``<html>``/``<head>`` corrupts the shell, a ROADMAP-locked
    anti-pattern); a direct or bookmark navigation gets the full ``shell/shell.html``
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
    }
    if stage == "analyze":
        context.update(await build_dashboard_context(request.app.state, session))
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
        agents_stmt = select(Agent).where(Agent.revoked_at.is_(None)).order_by(Agent.name)
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

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request=request, name="shell/_stage_fragment.html", context=context)
    return templates.TemplateResponse(request=request, name="shell/shell.html", context=context)


@router.get("/", response_class=HTMLResponse)
async def shell_home(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """GET / -- the shell root with Analyze selected by default (SHELL-01, D-02 bare root)."""
    return await _render_stage(request, "analyze", session)


@router.get("/s/{stage}", response_class=HTMLResponse)
async def shell_stage(request: Request, stage: str, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """GET /s/{stage} -- a single rail-node workspace.

    ``stage`` is whitelisted against ``STAGE_PARTIALS`` (D-02 per-stage validation owned
    here); an unknown stage 404s and is NEVER used to build a template path (T-57-01).
    """
    if stage not in STAGE_PARTIALS:
        raise HTTPException(status_code=404)
    return await _render_stage(request, stage, session)
