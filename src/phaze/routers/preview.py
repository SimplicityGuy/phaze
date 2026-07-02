"""Preview UI router -- redirects the legacy tree-preview route into the v7.0 shell."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse


router = APIRouter(tags=["preview"])


@router.get("/preview/", response_class=HTMLResponse)
async def tree_preview() -> RedirectResponse:
    """Redirect the legacy directory-tree preview into the v7.0 shell Move workspace.

    CUT-02 (Phase 62 / D-03b, D-05): the tree-preview page is superseded by the shell's
    Move workspace (``/s/move``). Phase 57 (SHELL-05) already 302-redirected non-HX GETs
    here and there was no live in-page HX consumer, so the whole page render was dead code.
    The route is retained as a pure 302 redirect so old bookmarks keep resolving.
    """
    return RedirectResponse(url="/s/move", status_code=302)
